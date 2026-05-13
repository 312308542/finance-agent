"""M0 仓储层。

仓储层只负责数据库读写和幂等更新，不承载采集、因子计算或推荐决策逻辑。
服务层后续可以组合这些仓储来跑通完整推荐链路。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    AssetORM,
    AssetUniverseMemberORM,
    AssetUniverseORM,
    MarketBarORM,
)

JsonDict = dict[str, Any]


class AssetRepository:
    """资产主数据仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_asset(
        self,
        *,
        asset_id: str,
        symbol: str,
        name: str,
        market: str,
        asset_type: str,
        exchange: str | None = None,
        currency: str | None = None,
        sector: str | None = None,
        base_asset: str | None = None,
        quote_asset: str | None = None,
        tradable: bool = True,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> AssetORM:
        """按 `asset_id` 幂等写入资产。"""

        values = {
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "market": market,
            "asset_type": asset_type,
            "exchange": exchange,
            "currency": currency,
            "sector": sector,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "tradable": tradable,
            "status": status,
            "payload": payload or {},
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssetORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"asset_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetORM.asset_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_asset(asset_id)

    def get_asset(self, asset_id: str) -> AssetORM:
        """根据资产 ID 查询资产，不存在则抛错。"""

        return self.session.get_one(AssetORM, asset_id)

    def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[AssetORM]:
        """按市场查询资产列表。"""

        statement: Select[tuple[AssetORM]] = select(AssetORM).where(AssetORM.market == market)
        if only_tradable:
            statement = statement.where(AssetORM.tradable.is_(True))
        return list(self.session.scalars(statement.order_by(AssetORM.symbol)))


class UniverseRepository:
    """候选池仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_universe(
        self,
        *,
        universe_id: str,
        name: str,
        source: str,
        market: str,
        as_of: datetime,
        strategy_context: str | None = None,
        owner_id: str | None = None,
        visibility: str = "system",
        base_universe_id: str | None = None,
        total_before_filter: int | None = None,
        total_after_filter: int | None = None,
        filters: JsonDict | None = None,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> AssetUniverseORM:
        """按 `universe_id` 幂等写入候选池定义。"""

        values = {
            "universe_id": universe_id,
            "name": name,
            "source": source,
            "market": market,
            "strategy_context": strategy_context,
            "owner_id": owner_id,
            "visibility": visibility,
            "base_universe_id": base_universe_id,
            "total_before_filter": total_before_filter,
            "total_after_filter": total_after_filter,
            "filters": filters or {},
            "status": status,
            "as_of": as_of,
            "payload": payload or {},
        }
        statement = insert(AssetUniverseORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"universe_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetUniverseORM.universe_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_universe(universe_id)

    def upsert_member(
        self,
        *,
        member_id: str,
        universe_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        as_of: datetime,
        included: bool = True,
        removed_reason: str | None = None,
        rank_hint: int | None = None,
        payload: JsonDict | None = None,
    ) -> AssetUniverseMemberORM:
        """按 `universe_id + asset_id` 幂等写入候选池成员。"""

        values = {
            "id": member_id,
            "universe_id": universe_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "included": included,
            "removed_reason": removed_reason,
            "rank_hint": rank_hint,
            "as_of": as_of,
            "payload": payload or {},
        }
        statement = insert(AssetUniverseMemberORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"id", "universe_id", "asset_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_universe_members_universe_asset",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_member(universe_id=universe_id, asset_id=asset_id)

    def replace_members(
        self,
        *,
        universe_id: str,
        members: Sequence[dict[str, Any]],
    ) -> list[AssetUniverseMemberORM]:
        """批量写入候选池成员。

        第一版采用逐条 upsert，优先保证语义清晰。后续候选池规模变大时，
        可以改成批量 insert + on conflict。
        """

        saved: list[AssetUniverseMemberORM] = []
        for member in members:
            saved.append(self.upsert_member(universe_id=universe_id, **member))
        return saved

    def get_universe(self, universe_id: str) -> AssetUniverseORM:
        """根据候选池 ID 查询候选池。"""

        return self.session.get_one(AssetUniverseORM, universe_id)

    def get_member(self, *, universe_id: str, asset_id: str) -> AssetUniverseMemberORM:
        """根据候选池和资产 ID 查询成员。"""

        statement = select(AssetUniverseMemberORM).where(
            AssetUniverseMemberORM.universe_id == universe_id,
            AssetUniverseMemberORM.asset_id == asset_id,
        )
        return self.session.scalars(statement).one()

    def list_members(
        self, universe_id: str, *, included_only: bool = True
    ) -> list[AssetUniverseMemberORM]:
        """查询候选池成员。"""

        statement = select(AssetUniverseMemberORM).where(
            AssetUniverseMemberORM.universe_id == universe_id
        )
        if included_only:
            statement = statement.where(AssetUniverseMemberORM.included.is_(True))
        return list(self.session.scalars(statement.order_by(AssetUniverseMemberORM.symbol)))


class MarketDataRepository:
    """标准行情仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_bar(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        timestamp: datetime,
        open_price: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: Decimal,
        source: str,
        adjustment: str = "",
        end_timestamp: datetime | None = None,
        amount: Decimal | None = None,
        is_closed: bool = True,
        raw_record_id: str | None = None,
        status: str = "available",
    ) -> MarketBarORM:
        """按 K 线唯一键幂等写入行情。"""

        values = {
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "timeframe": timeframe,
            "timestamp": timestamp,
            "end_timestamp": end_timestamp,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
            "source": source,
            "adjustment": adjustment,
            "is_closed": is_closed,
            "raw_record_id": raw_record_id,
            "status": status,
        }
        statement = insert(MarketBarORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"asset_id", "timeframe", "timestamp", "source", "adjustment"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    MarketBarORM.asset_id,
                    MarketBarORM.timeframe,
                    MarketBarORM.timestamp,
                    MarketBarORM.source,
                    MarketBarORM.adjustment,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_bar(
            asset_id=asset_id,
            timeframe=timeframe,
            timestamp=timestamp,
            source=source,
            adjustment=adjustment,
        )

    def get_bar(
        self,
        *,
        asset_id: str,
        timeframe: str,
        timestamp: datetime,
        source: str,
        adjustment: str = "",
    ) -> MarketBarORM:
        """根据复合键查询单根 K 线。"""

        return self.session.get_one(
            MarketBarORM,
            {
                "asset_id": asset_id,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "source": source,
                "adjustment": adjustment,
            },
        )

    def list_recent_bars(
        self,
        *,
        asset_id: str,
        timeframe: str,
        limit: int,
        source: str | None = None,
        closed_only: bool = True,
    ) -> list[MarketBarORM]:
        """查询单标的最近 N 根 K 线，返回时间升序结果。"""

        statement = select(MarketBarORM).where(
            MarketBarORM.asset_id == asset_id,
            MarketBarORM.timeframe == timeframe,
        )
        if source:
            statement = statement.where(MarketBarORM.source == source)
        if closed_only:
            statement = statement.where(MarketBarORM.is_closed.is_(True))

        rows = list(
            self.session.scalars(statement.order_by(MarketBarORM.timestamp.desc()).limit(limit))
        )
        return list(reversed(rows))

    def list_window_bars(
        self,
        *,
        asset_ids: Sequence[str],
        timeframe: str,
        start_at: datetime,
        end_at: datetime,
        source: str | None = None,
    ) -> list[MarketBarORM]:
        """批量查询一组资产在时间窗口内的 K 线。"""

        statement = select(MarketBarORM).where(
            MarketBarORM.asset_id.in_(asset_ids),
            MarketBarORM.timeframe == timeframe,
            MarketBarORM.timestamp >= start_at,
            MarketBarORM.timestamp < end_at,
        )
        if source:
            statement = statement.where(MarketBarORM.source == source)
        return list(
            self.session.scalars(
                statement.order_by(MarketBarORM.asset_id, MarketBarORM.timestamp)
            )
        )
