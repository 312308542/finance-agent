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
    CapitalFlowSnapshotORM,
    CryptoDerivativeSnapshotORM,
    EventRecordORM,
    EvidenceORM,
    FundamentalSnapshotORM,
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


class FundamentalDataRepository:
    """A 股财务估值快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_fundamental_snapshot(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        symbol: str,
        source: str,
        status: str,
        as_of: datetime,
        report_period: str | None = None,
        pe_ttm: Decimal | None = None,
        pb: Decimal | None = None,
        roe: Decimal | None = None,
        revenue_growth_yoy: Decimal | None = None,
        net_profit_growth_yoy: Decimal | None = None,
        debt_to_asset: Decimal | None = None,
        operating_cashflow: Decimal | None = None,
        missing_fields: list[str] | None = None,
        payload: JsonDict | None = None,
    ) -> FundamentalSnapshotORM:
        """按 `snapshot_id` 幂等写入财务估值快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "report_period": report_period,
            "pe_ttm": pe_ttm,
            "pb": pb,
            "roe": roe,
            "revenue_growth_yoy": revenue_growth_yoy,
            "net_profit_growth_yoy": net_profit_growth_yoy,
            "debt_to_asset": debt_to_asset,
            "operating_cashflow": operating_cashflow,
            "source": source,
            "status": status,
            "missing_fields": missing_fields or [],
            "as_of": as_of,
            "payload": payload or {},
        }
        statement = insert(FundamentalSnapshotORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "snapshot_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[FundamentalSnapshotORM.snapshot_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(FundamentalSnapshotORM, snapshot_id)


class CapitalFlowRepository:
    """A 股资金流快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_capital_flow_snapshot(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        window: str,
        source: str,
        status: str,
        as_of: datetime,
        main_net_inflow: Decimal | None = None,
        northbound_net_inflow: Decimal | None = None,
        turnover_rate: Decimal | None = None,
        amount: Decimal | None = None,
        payload: JsonDict | None = None,
    ) -> CapitalFlowSnapshotORM:
        """按 `snapshot_id` 幂等写入资金流快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "main_net_inflow": main_net_inflow,
            "northbound_net_inflow": northbound_net_inflow,
            "turnover_rate": turnover_rate,
            "amount": amount,
            "window": window,
            "source": source,
            "status": status,
            "as_of": as_of,
            "payload": payload or {},
        }
        statement = insert(CapitalFlowSnapshotORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "snapshot_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[CapitalFlowSnapshotORM.snapshot_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(CapitalFlowSnapshotORM, snapshot_id)


class EventRepository:
    """事件和证据仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_event(
        self,
        *,
        event_id: str,
        market: str,
        event_type: str,
        title: str,
        sentiment: str,
        importance: str,
        source: str,
        collected_at: datetime,
        asset_id: str | None = None,
        symbol: str | None = None,
        summary: str | None = None,
        url: str | None = None,
        published_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> EventRecordORM:
        """按 `event_id` 幂等写入事件记录。"""

        values = {
            "event_id": event_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "event_type": event_type,
            "title": title,
            "summary": summary,
            "sentiment": sentiment,
            "importance": importance,
            "source": source,
            "url": url,
            "published_at": published_at,
            "collected_at": collected_at,
            "payload": payload or {},
        }
        statement = insert(EventRecordORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "event_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[EventRecordORM.event_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(EventRecordORM, event_id)

    def upsert_evidence(
        self,
        *,
        evidence_id: str,
        evidence_type: str,
        source: str,
        title: str,
        reliability: str,
        collected_at: datetime,
        asset_id: str | None = None,
        summary: str | None = None,
        data_ref: str | None = None,
        url: str | None = None,
        as_of: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> EvidenceORM:
        """按 `evidence_id` 幂等写入证据索引。"""

        values = {
            "evidence_id": evidence_id,
            "evidence_type": evidence_type,
            "asset_id": asset_id,
            "source": source,
            "title": title,
            "summary": summary,
            "data_ref": data_ref,
            "url": url,
            "reliability": reliability,
            "as_of": as_of,
            "collected_at": collected_at,
            "payload": payload or {},
        }
        statement = insert(EvidenceORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "evidence_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[EvidenceORM.evidence_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(EvidenceORM, evidence_id)


class DerivativeDataRepository:
    """数字货币衍生品快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_crypto_derivative_snapshot(
        self,
        *,
        snapshot_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        source: str,
        as_of: datetime,
        funding_rate: Decimal | None = None,
        next_funding_time: datetime | None = None,
        open_interest: Decimal | None = None,
        open_interest_value: Decimal | None = None,
        long_short_ratio: Decimal | None = None,
        basis_rate: Decimal | None = None,
        liquidation_risk_score: Decimal | None = None,
        status: str = "available",
        payload: JsonDict | None = None,
    ) -> CryptoDerivativeSnapshotORM:
        """按 `asset_id + as_of + source` 幂等写入衍生品快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "source": source,
            "as_of": as_of,
            "funding_rate": funding_rate,
            "next_funding_time": next_funding_time,
            "open_interest": open_interest,
            "open_interest_value": open_interest_value,
            "long_short_ratio": long_short_ratio,
            "basis_rate": basis_rate,
            "liquidation_risk_score": liquidation_risk_score,
            "status": status,
            "payload": payload or {},
        }
        statement = insert(CryptoDerivativeSnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"asset_id", "as_of", "source"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    CryptoDerivativeSnapshotORM.asset_id,
                    CryptoDerivativeSnapshotORM.as_of,
                    CryptoDerivativeSnapshotORM.source,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_snapshot(asset_id=asset_id, as_of=as_of, source=source)

    def get_snapshot(
        self,
        *,
        asset_id: str,
        as_of: datetime,
        source: str,
    ) -> CryptoDerivativeSnapshotORM:
        """根据复合键查询单条衍生品快照。"""

        return self.session.get_one(
            CryptoDerivativeSnapshotORM,
            {
                "asset_id": asset_id,
                "as_of": as_of,
                "source": source,
            },
        )

    def get_latest_snapshot(
        self,
        *,
        asset_id: str,
        source: str | None = None,
    ) -> CryptoDerivativeSnapshotORM | None:
        """查询单标的最新衍生品快照。"""

        statement = select(CryptoDerivativeSnapshotORM).where(
            CryptoDerivativeSnapshotORM.asset_id == asset_id
        )
        if source:
            statement = statement.where(CryptoDerivativeSnapshotORM.source == source)
        return self.session.scalars(
            statement.order_by(CryptoDerivativeSnapshotORM.as_of.desc()).limit(1)
        ).one_or_none()

    def list_recent_snapshots(
        self,
        *,
        asset_id: str,
        limit: int,
        source: str | None = None,
    ) -> list[CryptoDerivativeSnapshotORM]:
        """查询单标的最近 N 条衍生品快照，返回时间升序结果。"""

        statement = select(CryptoDerivativeSnapshotORM).where(
            CryptoDerivativeSnapshotORM.asset_id == asset_id
        )
        if source:
            statement = statement.where(CryptoDerivativeSnapshotORM.source == source)
        rows = list(
            self.session.scalars(
                statement.order_by(CryptoDerivativeSnapshotORM.as_of.desc()).limit(limit)
            )
        )
        return list(reversed(rows))
