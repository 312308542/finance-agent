"""M0 仓储层。

仓储层只负责数据库读写和幂等更新，不承载采集、因子计算或推荐决策逻辑。
服务层后续可以组合这些仓储来跑通完整推荐链路。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    AssetORM,
    AssetRecommendationORM,
    AssetScoreORM,
    AssetUniverseMemberORM,
    AssetUniverseORM,
    CapitalFlowSnapshotORM,
    CryptoDerivativeSnapshotORM,
    EventRecordORM,
    EvidenceORM,
    FactorFrameORM,
    FundamentalSnapshotORM,
    IndicatorFrameORM,
    MarketBarORM,
    RawRecordORM,
    RecommendationRunORM,
    RecommendationRunUniverseORM,
    RiskFindingORM,
    ScreeningResultItemORM,
    ScreeningResultORM,
    SignalSnapshotORM,
)

JsonDict = dict[str, Any]


def _json_safe(value: Any) -> Any:
    """把第三方响应转换成 PostgreSQL JSONB 可接受的结构。"""

    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            return str(value)
    return str(value)


def _stable_json_hash(value: Any) -> str:
    """按稳定 JSON 表达计算 sha1，便于请求和内容追溯。"""

    safe_value = _json_safe(value)
    encoded = json.dumps(
        safe_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


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
            "payload": _json_safe(payload or {}),
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

    def get_asset_or_none(self, asset_id: str) -> AssetORM | None:
        """根据资产 ID 查询资产，不存在时返回空。"""

        return self.session.get(AssetORM, asset_id)

    def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[AssetORM]:
        """按市场查询资产列表。"""

        statement: Select[tuple[AssetORM]] = select(AssetORM).where(AssetORM.market == market)
        if only_tradable:
            statement = statement.where(AssetORM.tradable.is_(True))
        return list(self.session.scalars(statement.order_by(AssetORM.symbol)))


class RawRecordRepository:
    """Provider 原始响应归档仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_raw_record(
        self,
        *,
        provider: str,
        endpoint: str,
        request_params: JsonDict,
        response_payload: JsonDict,
        status: str,
        collected_at: datetime,
        asset_id: str | None = None,
        symbol: str | None = None,
        market: str | None = None,
        provider_version: str | None = None,
        latency_ms: int | None = None,
        retry_count: int = 0,
        error_message: str | None = None,
        as_of: datetime | None = None,
        raw_record_id: str | None = None,
    ) -> RawRecordORM:
        """追加写入一条原始响应记录。

        `raw_records` 是审计表，默认不按请求覆盖历史。调用方如需要幂等，
        可以显式传入 `raw_record_id`。
        """

        safe_request = _json_safe(request_params)
        safe_response = _json_safe(response_payload)
        request_hash = _stable_json_hash(safe_request)
        content_hash = _stable_json_hash(safe_response)
        record_id = raw_record_id or (
            f"raw:{provider}:{endpoint}:{collected_at.isoformat()}:{request_hash[:12]}:"
            f"{content_hash[:12]}"
        )
        values = {
            "raw_record_id": record_id,
            "provider": provider,
            "endpoint": endpoint,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "request_params": safe_request,
            "request_hash": request_hash,
            "response_payload": safe_response,
            "content_hash": content_hash,
            "provider_version": provider_version,
            "latency_ms": latency_ms,
            "retry_count": retry_count,
            "status": status,
            "error_message": error_message,
            "as_of": as_of,
            "collected_at": collected_at,
        }
        self.session.execute(insert(RawRecordORM).values(**values))
        self.session.flush()
        return self.session.get_one(RawRecordORM, record_id)

    def count_by_provider(self, provider: str) -> int:
        """统计某个 Provider 已归档的原始记录数量。"""

        statement = select(func.count()).select_from(RawRecordORM).where(
            RawRecordORM.provider == provider
        )
        return int(self.session.scalar(statement) or 0)


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
            "filters": _json_safe(filters or {}),
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
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
            "payload": _json_safe(payload or {}),
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


class IndicatorFrameRepository:
    """技术指标结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_indicator_frame(
        self,
        *,
        indicator_frame_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        horizon: str,
        library: str,
        input_start_at: datetime,
        input_end_at: datetime,
        bar_count: int,
        status: str,
        as_of: datetime,
        library_version: str | None = None,
        rsi_14: Decimal | None = None,
        macd: Decimal | None = None,
        macd_signal: Decimal | None = None,
        macd_hist: Decimal | None = None,
        atr_14: Decimal | None = None,
        bb_percent_b: Decimal | None = None,
        ma_20: Decimal | None = None,
        ma_60: Decimal | None = None,
        payload: JsonDict | None = None,
    ) -> IndicatorFrameORM:
        """按指标输入唯一键幂等写入技术指标结果。"""

        values = {
            "indicator_frame_id": indicator_frame_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "timeframe": timeframe,
            "horizon": horizon,
            "library": library,
            "library_version": library_version,
            "input_start_at": input_start_at,
            "input_end_at": input_end_at,
            "bar_count": bar_count,
            "rsi_14": rsi_14,
            "macd": macd,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "atr_14": atr_14,
            "bb_percent_b": bb_percent_b,
            "ma_20": ma_20,
            "ma_60": ma_60,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(IndicatorFrameORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key
            not in {
                "indicator_frame_id",
                "asset_id",
                "timeframe",
                "horizon",
                "library",
                "input_end_at",
            }
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_indicator_frames_input",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_latest_indicator_frame(
            asset_id=asset_id,
            timeframe=timeframe,
            horizon=horizon,
            library=library,
        )

    def get_latest_indicator_frame(
        self,
        *,
        asset_id: str,
        timeframe: str,
        horizon: str,
        library: str | None = None,
    ) -> IndicatorFrameORM | None:
        """查询单标的最新技术指标结果。"""

        statement = select(IndicatorFrameORM).where(
            IndicatorFrameORM.asset_id == asset_id,
            IndicatorFrameORM.timeframe == timeframe,
            IndicatorFrameORM.horizon == horizon,
        )
        if library:
            statement = statement.where(IndicatorFrameORM.library == library)
        return self.session.scalars(
            statement.order_by(IndicatorFrameORM.input_end_at.desc()).limit(1)
        ).one_or_none()


class FactorFrameRepository:
    """推荐因子结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_factor_frame(
        self,
        *,
        factor_frame_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        horizon: str,
        status: str,
        total_available_groups: int,
        missing_groups: list[str],
        source_ids: list[str],
        as_of: datetime,
        indicator_frame_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> FactorFrameORM:
        """按 `factor_frame_id` 幂等写入因子结果。"""

        values = {
            "factor_frame_id": factor_frame_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "horizon": horizon,
            "status": status,
            "total_available_groups": total_available_groups,
            "missing_groups": missing_groups,
            "source_ids": source_ids,
            "indicator_frame_id": indicator_frame_id,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(FactorFrameORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "factor_frame_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[FactorFrameORM.factor_frame_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(FactorFrameORM, factor_frame_id)

    def get_latest_factor_frame(
        self,
        *,
        asset_id: str,
        horizon: str,
    ) -> FactorFrameORM | None:
        """查询单标的最新因子结果。"""

        statement = select(FactorFrameORM).where(
            FactorFrameORM.asset_id == asset_id,
            FactorFrameORM.horizon == horizon,
        )
        return self.session.scalars(
            statement.order_by(FactorFrameORM.as_of.desc()).limit(1)
        ).one_or_none()


class ScreeningRepository:
    """候选池初筛结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_screening_result(
        self,
        *,
        screening_id: str,
        universe_id: str,
        strategy: str,
        market: str,
        passed_count: int,
        removed_count: int,
        rules: JsonDict,
        status: str,
        as_of: datetime,
        payload: JsonDict | None = None,
    ) -> ScreeningResultORM:
        """按 `screening_id` 幂等写入初筛汇总。"""

        values = {
            "screening_id": screening_id,
            "universe_id": universe_id,
            "strategy": strategy,
            "market": market,
            "passed_count": passed_count,
            "removed_count": removed_count,
            "rules": _json_safe(rules),
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(ScreeningResultORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "screening_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[ScreeningResultORM.screening_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(ScreeningResultORM, screening_id)

    def upsert_screening_item(
        self,
        *,
        screening_item_id: str,
        screening_id: str,
        universe_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        passed: bool,
        data_status: str,
        as_of: datetime,
        removed_reason: str | None = None,
        failed_rules: list[str] | None = None,
        passed_rules: list[str] | None = None,
        liquidity_status: str | None = None,
        payload: JsonDict | None = None,
    ) -> ScreeningResultItemORM:
        """按 `screening_id + asset_id` 幂等写入初筛明细。"""

        values = {
            "screening_item_id": screening_item_id,
            "screening_id": screening_id,
            "universe_id": universe_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "passed": passed,
            "removed_reason": removed_reason,
            "failed_rules": failed_rules or [],
            "passed_rules": passed_rules or [],
            "data_status": data_status,
            "liquidity_status": liquidity_status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(ScreeningResultItemORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"screening_item_id", "screening_id", "asset_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_screening_items_screening_asset",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_screening_item(screening_id=screening_id, asset_id=asset_id)

    def get_screening_result(self, screening_id: str) -> ScreeningResultORM:
        """查询初筛汇总。"""

        return self.session.get_one(ScreeningResultORM, screening_id)

    def get_screening_item(
        self,
        *,
        screening_id: str,
        asset_id: str,
    ) -> ScreeningResultItemORM:
        """查询单条初筛明细。"""

        statement = select(ScreeningResultItemORM).where(
            ScreeningResultItemORM.screening_id == screening_id,
            ScreeningResultItemORM.asset_id == asset_id,
        )
        return self.session.scalars(statement).one()

    def list_items(
        self,
        *,
        screening_id: str,
        passed_only: bool = False,
    ) -> list[ScreeningResultItemORM]:
        """查询初筛明细。"""

        statement = select(ScreeningResultItemORM).where(
            ScreeningResultItemORM.screening_id == screening_id
        )
        if passed_only:
            statement = statement.where(ScreeningResultItemORM.passed.is_(True))
        return list(self.session.scalars(statement.order_by(ScreeningResultItemORM.symbol)))


class AssetScoreRepository:
    """标的透明评分仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_asset_score(
        self,
        *,
        score_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        universe_id: str,
        screening_id: str,
        factor_frame_id: str,
        horizon: str,
        total_score: Decimal,
        rank: int,
        confidence: Decimal,
        rule_version: str,
        status: str,
        as_of: datetime,
        risk_penalty: Decimal,
        missing_penalty: Decimal,
        technical_score: Decimal | None = None,
        fundamental_score: Decimal | None = None,
        valuation_score: Decimal | None = None,
        flow_score: Decimal | None = None,
        derivatives_score: Decimal | None = None,
        event_score: Decimal | None = None,
        rank_in_universe: int | None = None,
        payload: JsonDict | None = None,
    ) -> AssetScoreORM:
        """按 `score_id` 幂等写入评分结果。"""

        values = {
            "score_id": score_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "universe_id": universe_id,
            "screening_id": screening_id,
            "factor_frame_id": factor_frame_id,
            "horizon": horizon,
            "total_score": total_score,
            "technical_score": technical_score,
            "fundamental_score": fundamental_score,
            "valuation_score": valuation_score,
            "flow_score": flow_score,
            "derivatives_score": derivatives_score,
            "event_score": event_score,
            "risk_penalty": risk_penalty,
            "rank": rank,
            "rank_in_universe": rank_in_universe,
            "confidence": confidence,
            "missing_penalty": missing_penalty,
            "rule_version": rule_version,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssetScoreORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "score_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetScoreORM.score_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssetScoreORM, score_id)

    def list_scores_for_screening(self, screening_id: str) -> list[AssetScoreORM]:
        """查询一次初筛对应的评分结果。"""

        statement = (
            select(AssetScoreORM)
            .where(AssetScoreORM.screening_id == screening_id)
            .order_by(AssetScoreORM.rank)
        )
        return list(self.session.scalars(statement))


class SignalSnapshotRepository:
    """信号快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_signal_snapshot(
        self,
        *,
        signal_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        horizon: str,
        direction: str,
        score: Decimal,
        confidence: Decimal,
        rule_version: str,
        status: str,
        as_of: datetime,
        payload: JsonDict | None = None,
    ) -> SignalSnapshotORM:
        """按 `signal_id` 幂等写入信号快照。"""

        values = {
            "signal_id": signal_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "horizon": horizon,
            "direction": direction,
            "score": score,
            "confidence": confidence,
            "rule_version": rule_version,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(SignalSnapshotORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "signal_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[SignalSnapshotORM.signal_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(SignalSnapshotORM, signal_id)

    def get_latest_signal(
        self,
        *,
        asset_id: str,
        horizon: str,
    ) -> SignalSnapshotORM | None:
        """查询单标的最新信号。"""

        statement = select(SignalSnapshotORM).where(
            SignalSnapshotORM.asset_id == asset_id,
            SignalSnapshotORM.horizon == horizon,
        )
        return self.session.scalars(
            statement.order_by(SignalSnapshotORM.as_of.desc()).limit(1)
        ).one_or_none()


class RecommendationRepository:
    """推荐运行和单标的推荐结果仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_run(
        self,
        *,
        run_id: str,
        strategy: str,
        market: str,
        horizon: str,
        limit: int,
        status: str,
        started_at: datetime,
        universe_id: str | None = None,
        screening_id: str | None = None,
        finished_at: datetime | None = None,
        summary: str | None = None,
        payload: JsonDict | None = None,
    ) -> RecommendationRunORM:
        """按 `run_id` 幂等写入推荐运行。"""

        values = {
            "run_id": run_id,
            "universe_id": universe_id,
            "screening_id": screening_id,
            "strategy": strategy,
            "market": market,
            "horizon": horizon,
            "limit": limit,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "summary": summary,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(RecommendationRunORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "run_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[RecommendationRunORM.run_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(RecommendationRunORM, run_id)

    def upsert_run_universe(
        self,
        *,
        record_id: str,
        run_id: str,
        universe_id: str,
        market: str,
        role: str,
        weight: Decimal | None = None,
        asset_count: int | None = None,
        payload: JsonDict | None = None,
    ) -> RecommendationRunUniverseORM:
        """按 `run_id + universe_id` 幂等写入推荐运行候选池关联。"""

        values = {
            "id": record_id,
            "run_id": run_id,
            "universe_id": universe_id,
            "market": market,
            "role": role,
            "weight": weight,
            "asset_count": asset_count,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(RecommendationRunUniverseORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"id", "run_id", "universe_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_run_universes_run_universe",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_run_universe(run_id=run_id, universe_id=universe_id)

    def upsert_asset_recommendation(
        self,
        *,
        recommendation_id: str,
        run_id: str,
        asset_id: str,
        symbol: str,
        name: str,
        market: str,
        horizon: str,
        action: str,
        rank: int,
        total_score: Decimal,
        confidence: Decimal,
        conviction: str,
        score_id: str | None = None,
        factor_frame_id: str | None = None,
        signal_ids: list[str] | None = None,
        risk_ids: list[str] | None = None,
        agent_analysis_item_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        watch_conditions: JsonDict | None = None,
        invalid_if: JsonDict | None = None,
        summary: str | None = None,
        payload: JsonDict | None = None,
    ) -> AssetRecommendationORM:
        """按 `recommendation_id` 幂等写入单标的推荐结果。"""

        values = {
            "recommendation_id": recommendation_id,
            "run_id": run_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "name": name,
            "market": market,
            "horizon": horizon,
            "action": action,
            "rank": rank,
            "total_score": total_score,
            "confidence": confidence,
            "conviction": conviction,
            "score_id": score_id,
            "factor_frame_id": factor_frame_id,
            "signal_ids": signal_ids or [],
            "risk_ids": risk_ids or [],
            "agent_analysis_item_ids": agent_analysis_item_ids or [],
            "evidence_ids": evidence_ids or [],
            "watch_conditions": _json_safe(watch_conditions or {}),
            "invalid_if": _json_safe(invalid_if or {}),
            "summary": summary,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssetRecommendationORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key != "recommendation_id"
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetRecommendationORM.recommendation_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssetRecommendationORM, recommendation_id)

    def get_run_universe(
        self,
        *,
        run_id: str,
        universe_id: str,
    ) -> RecommendationRunUniverseORM:
        """查询推荐运行候选池关联。"""

        statement = select(RecommendationRunUniverseORM).where(
            RecommendationRunUniverseORM.run_id == run_id,
            RecommendationRunUniverseORM.universe_id == universe_id,
        )
        return self.session.scalars(statement).one()

    def list_recommendations(self, run_id: str) -> list[AssetRecommendationORM]:
        """查询一次推荐运行的推荐结果。"""

        statement = (
            select(AssetRecommendationORM)
            .where(AssetRecommendationORM.run_id == run_id)
            .order_by(AssetRecommendationORM.rank)
        )
        return list(self.session.scalars(statement))


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
            "payload": _json_safe(payload or {}),
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

    def get_latest_snapshot(self, *, asset_id: str) -> FundamentalSnapshotORM | None:
        """查询单标的最新财务估值快照。"""

        statement = select(FundamentalSnapshotORM).where(
            FundamentalSnapshotORM.asset_id == asset_id
        )
        return self.session.scalars(
            statement.order_by(FundamentalSnapshotORM.as_of.desc()).limit(1)
        ).one_or_none()


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
            "payload": _json_safe(payload or {}),
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

    def get_latest_snapshot(
        self,
        *,
        asset_id: str,
        window: str | None = None,
    ) -> CapitalFlowSnapshotORM | None:
        """查询单标的最新资金流快照。"""

        statement = select(CapitalFlowSnapshotORM).where(
            CapitalFlowSnapshotORM.asset_id == asset_id
        )
        if window:
            statement = statement.where(CapitalFlowSnapshotORM.window == window)
        return self.session.scalars(
            statement.order_by(CapitalFlowSnapshotORM.as_of.desc()).limit(1)
        ).one_or_none()


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
            "payload": _json_safe(payload or {}),
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
            "payload": _json_safe(payload or {}),
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

    def list_recent_events(self, *, asset_id: str, limit: int = 20) -> list[EventRecordORM]:
        """查询单标的最近事件。"""

        statement = (
            select(EventRecordORM)
            .where(EventRecordORM.asset_id == asset_id)
            .order_by(
                EventRecordORM.published_at.desc().nullslast(),
                EventRecordORM.collected_at.desc(),
            )
            .limit(limit)
        )
        return list(self.session.scalars(statement))


class RiskRepository:
    """风险发现仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_risk_finding(
        self,
        *,
        risk_id: str,
        scope: str,
        risk_type: str,
        severity: str,
        title: str,
        as_of: datetime,
        asset_id: str | None = None,
        score: Decimal | None = None,
        description: str | None = None,
        evidence_ids: list[str] | None = None,
        payload: JsonDict | None = None,
    ) -> RiskFindingORM:
        """按 `risk_id` 幂等写入风险发现。"""

        values = {
            "risk_id": risk_id,
            "asset_id": asset_id,
            "scope": scope,
            "risk_type": risk_type,
            "severity": severity,
            "score": score,
            "title": title,
            "description": description,
            "as_of": as_of,
            "evidence_ids": evidence_ids or [],
            "payload": _json_safe(payload or {}),
        }
        statement = insert(RiskFindingORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "risk_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[RiskFindingORM.risk_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(RiskFindingORM, risk_id)

    def list_recent_risks(self, *, asset_id: str, limit: int = 20) -> list[RiskFindingORM]:
        """查询单标的最近风险发现。"""

        statement = (
            select(RiskFindingORM)
            .where(RiskFindingORM.asset_id == asset_id)
            .order_by(RiskFindingORM.as_of.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


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
            "payload": _json_safe(payload or {}),
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
