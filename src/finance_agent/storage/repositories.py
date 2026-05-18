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
    AgentWorkflowEventORM,
    AgentWorkflowRunORM,
    AssetORM,
    AssetRecommendationORM,
    AssetScoreORM,
    AssetThesisORM,
    AssetUniverseMemberORM,
    AssetUniverseORM,
    AssistantMemoryORM,
    AssistantTriggerEventORM,
    CapitalFlowSnapshotORM,
    CryptoDerivativeSnapshotORM,
    DataQualitySnapshotORM,
    DecisionLogORM,
    EventRecordORM,
    EvidenceORM,
    FactorFrameORM,
    FinancialMemoryEdgeORM,
    FundamentalSnapshotORM,
    IndicatorFrameORM,
    MarketBarORM,
    MemoryEmbeddingORM,
    MonitoringAlertORM,
    PortfolioORM,
    PortfolioSnapshotORM,
    PositionORM,
    PositionSnapshotORM,
    RawRecordORM,
    RecommendationRunORM,
    RecommendationRunUniverseORM,
    ReviewTaskORM,
    RiskFindingORM,
    ScreeningResultItemORM,
    ScreeningResultORM,
    SignalSnapshotORM,
    WatchlistItemEventORM,
    WatchlistItemORM,
    WatchlistORM,
)

JsonDict = dict[str, Any]


def _ensure_not_mixed_market(market: str, *, context: str) -> None:
    """推荐链路中的候选池和推荐结果必须属于单一市场。"""

    if market == "mixed":
        raise ValueError(f"{context} 不能使用 mixed，A 股和数字货币必须走两条独立链路。")


def _ensure_same_market(
    *,
    expected: str,
    actual: str,
    context: str,
    subject: str,
) -> None:
    """校验同一次候选池或推荐运行中的市场一致性。"""

    if expected != actual:
        raise ValueError(
            f"{context} 市场为 {expected}，但 {subject} 属于 {actual}，不能跨市场混合。"
        )


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

        _ensure_not_mixed_market(market, context="候选池")
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

        _ensure_not_mixed_market(market, context="候选池成员")
        universe = self.get_universe(universe_id)
        _ensure_same_market(
            expected=universe.market,
            actual=market,
            context=f"候选池 {universe_id}",
            subject=f"成员 {asset_id}",
        )
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

    def get_latest_score(
        self,
        *,
        asset_id: str,
        horizon: str,
    ) -> AssetScoreORM | None:
        """查询单标的最新多维评分。"""

        statement = (
            select(AssetScoreORM)
            .where(
                AssetScoreORM.asset_id == asset_id,
                AssetScoreORM.horizon == horizon,
            )
            .order_by(AssetScoreORM.as_of.desc())
            .limit(1)
        )
        return self.session.scalars(statement).one_or_none()


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

    def list_recent_signals(
        self,
        *,
        asset_id: str,
        horizon: str,
        limit: int = 5,
    ) -> list[SignalSnapshotORM]:
        """查询单标的最近信号快照，返回时间倒序结果。"""

        statement = (
            select(SignalSnapshotORM)
            .where(
                SignalSnapshotORM.asset_id == asset_id,
                SignalSnapshotORM.horizon == horizon,
            )
            .order_by(SignalSnapshotORM.as_of.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


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

        _ensure_not_mixed_market(market, context="推荐运行")
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

        _ensure_not_mixed_market(market, context="推荐运行候选池关联")
        run = self.session.get(RecommendationRunORM, run_id)
        if run is not None:
            _ensure_same_market(
                expected=run.market,
                actual=market,
                context=f"推荐运行 {run_id}",
                subject=f"候选池 {universe_id}",
            )
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

        _ensure_not_mixed_market(market, context="推荐结果")
        run = self.session.get(RecommendationRunORM, run_id)
        if run is not None:
            _ensure_same_market(
                expected=run.market,
                actual=market,
                context=f"推荐运行 {run_id}",
                subject=f"推荐标的 {asset_id}",
            )
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

    def get_recommendation(self, recommendation_id: str) -> AssetRecommendationORM:
        """根据推荐 ID 查询单条推荐结果。"""

        return self.session.get_one(AssetRecommendationORM, recommendation_id)

    def list_top_recommendations(
        self,
        *,
        run_id: str,
        limit: int = 20,
    ) -> list[AssetRecommendationORM]:
        """查询一次推荐运行的前 N 条推荐结果。"""

        statement = (
            select(AssetRecommendationORM)
            .where(AssetRecommendationORM.run_id == run_id)
            .order_by(AssetRecommendationORM.rank)
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_available_runs_since(
        self,
        *,
        since: datetime,
        market: str | None = None,
        limit: int = 20,
    ) -> list[RecommendationRunORM]:
        """查询最近完成或可用的推荐运行。"""

        statement = select(RecommendationRunORM).where(
            RecommendationRunORM.status == "available",
            RecommendationRunORM.started_at >= since,
        )
        if market:
            statement = statement.where(RecommendationRunORM.market == market)
        return list(
            self.session.scalars(
                statement.order_by(RecommendationRunORM.started_at.desc()).limit(limit)
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

    def list_recent_snapshots(
        self,
        *,
        asset_id: str,
        limit: int,
        source: str | None = None,
    ) -> list[FundamentalSnapshotORM]:
        """查询单标的最近 N 条财务估值快照，返回时间升序结果。"""

        statement = select(FundamentalSnapshotORM).where(
            FundamentalSnapshotORM.asset_id == asset_id
        )
        if source:
            statement = statement.where(FundamentalSnapshotORM.source == source)
        rows = list(
            self.session.scalars(
                statement.order_by(FundamentalSnapshotORM.as_of.desc()).limit(limit)
            )
        )
        return list(reversed(rows))


class PortfolioRepository:
    """私人金融助手组合和持仓仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_portfolio(
        self,
        *,
        portfolio_id: str,
        owner_id: str,
        name: str,
        portfolio_type: str,
        base_currency: str,
        risk_profile: str,
        as_of: datetime,
        total_equity: Decimal | None = None,
        cash: Decimal | None = None,
        market_value: Decimal | None = None,
        max_position_weight: Decimal | None = None,
        max_drawdown_alert: Decimal | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> PortfolioORM:
        """按 `portfolio_id` 幂等写入组合定义。"""

        values = {
            "portfolio_id": portfolio_id,
            "owner_id": owner_id,
            "name": name,
            "portfolio_type": portfolio_type,
            "base_currency": base_currency,
            "risk_profile": risk_profile,
            "total_equity": total_equity,
            "cash": cash,
            "market_value": market_value,
            "max_position_weight": max_position_weight,
            "max_drawdown_alert": max_drawdown_alert,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(PortfolioORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "portfolio_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[PortfolioORM.portfolio_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(PortfolioORM, portfolio_id)

    def insert_portfolio_snapshot(
        self,
        *,
        snapshot_id: str,
        portfolio_id: str,
        owner_id: str,
        captured_at: datetime,
        source: str,
        total_equity: Decimal | None = None,
        cash: Decimal | None = None,
        market_value: Decimal | None = None,
        position_count: int | None = None,
        payload: JsonDict | None = None,
    ) -> PortfolioSnapshotORM:
        """写入组合历史快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "portfolio_id": portfolio_id,
            "owner_id": owner_id,
            "total_equity": total_equity,
            "cash": cash,
            "market_value": market_value,
            "position_count": position_count,
            "source": source,
            "captured_at": captured_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(PortfolioSnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"snapshot_id", "captured_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    PortfolioSnapshotORM.snapshot_id,
                    PortfolioSnapshotORM.captured_at,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            PortfolioSnapshotORM,
            {"snapshot_id": snapshot_id, "captured_at": captured_at},
        )

    def upsert_position(
        self,
        *,
        position_id: str,
        portfolio_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        side: str,
        quantity: Decimal,
        as_of: datetime,
        avg_cost: Decimal | None = None,
        last_price: Decimal | None = None,
        market_value: Decimal | None = None,
        unrealized_pnl: Decimal | None = None,
        unrealized_pnl_pct: Decimal | None = None,
        portfolio_weight: Decimal | None = None,
        leverage: Decimal | None = None,
        liquidation_price: Decimal | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> PositionORM:
        """按 `portfolio_id + asset_id + side` 幂等写入当前持仓。"""

        values = {
            "position_id": position_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "side": side,
            "quantity": quantity,
            "avg_cost": avg_cost,
            "last_price": last_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "portfolio_weight": portfolio_weight,
            "leverage": leverage,
            "liquidation_price": liquidation_price,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(PositionORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"position_id", "portfolio_id", "asset_id", "side"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_positions_portfolio_asset_side",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_position(portfolio_id=portfolio_id, asset_id=asset_id, side=side)

    def insert_position_snapshot(
        self,
        *,
        snapshot_id: str,
        position_id: str,
        portfolio_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        side: str,
        quantity: Decimal,
        captured_at: datetime,
        source: str,
        avg_cost: Decimal | None = None,
        last_price: Decimal | None = None,
        market_value: Decimal | None = None,
        unrealized_pnl: Decimal | None = None,
        unrealized_pnl_pct: Decimal | None = None,
        portfolio_weight: Decimal | None = None,
        leverage: Decimal | None = None,
        liquidation_price: Decimal | None = None,
        payload: JsonDict | None = None,
    ) -> PositionSnapshotORM:
        """写入持仓历史快照。"""

        values = {
            "snapshot_id": snapshot_id,
            "position_id": position_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "side": side,
            "quantity": quantity,
            "avg_cost": avg_cost,
            "last_price": last_price,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "portfolio_weight": portfolio_weight,
            "leverage": leverage,
            "liquidation_price": liquidation_price,
            "source": source,
            "captured_at": captured_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(PositionSnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"snapshot_id", "captured_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    PositionSnapshotORM.snapshot_id,
                    PositionSnapshotORM.captured_at,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            PositionSnapshotORM,
            {"snapshot_id": snapshot_id, "captured_at": captured_at},
        )

    def get_portfolio(self, portfolio_id: str) -> PortfolioORM:
        """根据组合 ID 查询组合。"""

        return self.session.get_one(PortfolioORM, portfolio_id)

    def list_portfolios(self, *, owner_id: str, status: str | None = None) -> list[PortfolioORM]:
        """查询用户组合。"""

        statement = select(PortfolioORM).where(PortfolioORM.owner_id == owner_id)
        if status:
            statement = statement.where(PortfolioORM.status == status)
        return list(self.session.scalars(statement.order_by(PortfolioORM.updated_at.desc())))

    def get_position(self, *, portfolio_id: str, asset_id: str, side: str) -> PositionORM:
        """根据组合、资产和方向查询持仓。"""

        statement = select(PositionORM).where(
            PositionORM.portfolio_id == portfolio_id,
            PositionORM.asset_id == asset_id,
            PositionORM.side == side,
        )
        return self.session.scalars(statement).one()

    def list_positions(
        self,
        portfolio_id: str,
        *,
        status: str | None = "active",
    ) -> list[PositionORM]:
        """查询组合当前持仓。"""

        statement = select(PositionORM).where(PositionORM.portfolio_id == portfolio_id)
        if status:
            statement = statement.where(PositionORM.status == status)
        return list(
            self.session.scalars(statement.order_by(PositionORM.market, PositionORM.symbol))
        )

    def list_portfolio_snapshots(
        self,
        *,
        portfolio_id: str,
        limit: int = 20,
    ) -> list[PortfolioSnapshotORM]:
        """查询组合历史快照，返回时间倒序结果。"""

        statement = (
            select(PortfolioSnapshotORM)
            .where(PortfolioSnapshotORM.portfolio_id == portfolio_id)
            .order_by(PortfolioSnapshotORM.captured_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def list_position_snapshots(
        self,
        *,
        portfolio_id: str,
        asset_id: str | None = None,
        limit: int = 20,
    ) -> list[PositionSnapshotORM]:
        """查询持仓历史快照，返回时间倒序结果。"""

        statement = select(PositionSnapshotORM).where(
            PositionSnapshotORM.portfolio_id == portfolio_id
        )
        if asset_id:
            statement = statement.where(PositionSnapshotORM.asset_id == asset_id)
        return list(
            self.session.scalars(
                statement.order_by(PositionSnapshotORM.captured_at.desc()).limit(limit)
            )
        )


class WatchlistRepository:
    """私人观察池和投资假设仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_watchlist(
        self,
        *,
        watchlist_id: str,
        owner_id: str,
        name: str,
        purpose: str,
        market: str | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> WatchlistORM:
        """按 `watchlist_id` 幂等写入观察池。"""

        values = {
            "watchlist_id": watchlist_id,
            "owner_id": owner_id,
            "name": name,
            "market": market,
            "purpose": purpose,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(WatchlistORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "watchlist_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[WatchlistORM.watchlist_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(WatchlistORM, watchlist_id)

    def upsert_watchlist_item(
        self,
        *,
        watchlist_item_id: str,
        watchlist_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        source_type: str,
        reason: str,
        source_id: str | None = None,
        watch_conditions: JsonDict | None = None,
        trigger_conditions: JsonDict | None = None,
        invalid_conditions: JsonDict | None = None,
        risk_level: str | None = None,
        status: str = "active",
        next_review_at: datetime | None = None,
        removed_at: datetime | None = None,
        removed_reason: str | None = None,
        payload: JsonDict | None = None,
    ) -> WatchlistItemORM:
        """按 `watchlist_id + asset_id` 幂等写入观察项。"""

        values = {
            "watchlist_item_id": watchlist_item_id,
            "watchlist_id": watchlist_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "source_type": source_type,
            "source_id": source_id,
            "reason": reason,
            "watch_conditions": _json_safe(watch_conditions or {}),
            "trigger_conditions": _json_safe(trigger_conditions or {}),
            "invalid_conditions": _json_safe(invalid_conditions or {}),
            "risk_level": risk_level,
            "status": status,
            "next_review_at": next_review_at,
            "removed_at": removed_at,
            "removed_reason": removed_reason,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(WatchlistItemORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"watchlist_item_id", "watchlist_id", "asset_id"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_watchlist_items_watchlist_asset",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_watchlist_item(watchlist_id=watchlist_id, asset_id=asset_id)

    def upsert_asset_thesis(
        self,
        *,
        thesis_id: str,
        asset_id: str,
        owner_id: str,
        source_type: str,
        thesis: str,
        source_id: str | None = None,
        supporting_points: list[JsonDict] | None = None,
        risk_points: list[JsonDict] | None = None,
        invalid_if: JsonDict | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> AssetThesisORM:
        """按 `thesis_id` 幂等写入投资假设。"""

        values = {
            "thesis_id": thesis_id,
            "asset_id": asset_id,
            "owner_id": owner_id,
            "source_type": source_type,
            "source_id": source_id,
            "thesis": thesis,
            "supporting_points": _json_safe(supporting_points or []),
            "risk_points": _json_safe(risk_points or []),
            "invalid_if": _json_safe(invalid_if or {}),
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssetThesisORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "thesis_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssetThesisORM.thesis_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssetThesisORM, thesis_id)

    def insert_watchlist_event(
        self,
        *,
        event_id: str,
        owner_id: str,
        watchlist_id: str,
        watchlist_item_id: str,
        asset_id: str,
        event_type: str,
        to_status: str,
        created_at: datetime,
        from_status: str | None = None,
        reason: str | None = None,
        source_decision_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> WatchlistItemEventORM:
        """写入观察池成员事件。"""

        values = {
            "event_id": event_id,
            "owner_id": owner_id,
            "watchlist_id": watchlist_id,
            "watchlist_item_id": watchlist_item_id,
            "asset_id": asset_id,
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "source_decision_id": source_decision_id,
            "created_at": created_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(WatchlistItemEventORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "event_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[WatchlistItemEventORM.event_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(WatchlistItemEventORM, event_id)

    def get_watchlist(self, watchlist_id: str) -> WatchlistORM:
        """根据观察池 ID 查询观察池。"""

        return self.session.get_one(WatchlistORM, watchlist_id)

    def get_watchlist_item(self, *, watchlist_id: str, asset_id: str) -> WatchlistItemORM:
        """根据观察池和资产查询观察项。"""

        statement = select(WatchlistItemORM).where(
            WatchlistItemORM.watchlist_id == watchlist_id,
            WatchlistItemORM.asset_id == asset_id,
        )
        return self.session.scalars(statement).one()

    def list_active_items(
        self,
        *,
        owner_id: str,
        watchlist_id: str | None = None,
    ) -> list[WatchlistItemORM]:
        """查询用户所有活跃观察项。"""

        statement = (
            select(WatchlistItemORM)
            .join(WatchlistORM, WatchlistORM.watchlist_id == WatchlistItemORM.watchlist_id)
            .where(WatchlistORM.owner_id == owner_id, WatchlistItemORM.status == "active")
        )
        if watchlist_id:
            statement = statement.where(WatchlistItemORM.watchlist_id == watchlist_id)
        return list(
            self.session.scalars(
                statement.order_by(
                    WatchlistItemORM.next_review_at.asc().nullslast(),
                    WatchlistItemORM.updated_at.desc(),
                )
            )
        )

    def list_asset_theses(
        self,
        *,
        owner_id: str,
        asset_id: str,
        status: str | None = "active",
    ) -> list[AssetThesisORM]:
        """查询单资产投资假设。"""

        statement = select(AssetThesisORM).where(
            AssetThesisORM.owner_id == owner_id,
            AssetThesisORM.asset_id == asset_id,
        )
        if status:
            statement = statement.where(AssetThesisORM.status == status)
        return list(self.session.scalars(statement.order_by(AssetThesisORM.updated_at.desc())))

    def list_watchlist_events(
        self,
        *,
        watchlist_id: str,
        limit: int = 50,
    ) -> list[WatchlistItemEventORM]:
        """查询观察池事件，返回时间倒序结果。"""

        statement = (
            select(WatchlistItemEventORM)
            .where(WatchlistItemEventORM.watchlist_id == watchlist_id)
            .order_by(WatchlistItemEventORM.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


class AssistantTriggerRepository:
    """私人金融助手触发事件仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_trigger_event(
        self,
        *,
        trigger_event_id: str,
        owner_id: str,
        trigger_type: str,
        dedup_key: str,
        severity: str,
        status: str,
        requested_workflow_type: str,
        triggered_at: datetime,
        trigger_ref: str | None = None,
        agent_runtime: str = "hermes_agent",
        agent_task_id: str | None = None,
        portfolio_id: str | None = None,
        watchlist_id: str | None = None,
        recommendation_run_id: str | None = None,
        asset_id: str | None = None,
        cooldown_until: datetime | None = None,
        dispatched_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> AssistantTriggerEventORM:
        """按触发事件 ID 幂等写入触发事件。"""

        values = {
            "trigger_event_id": trigger_event_id,
            "owner_id": owner_id,
            "trigger_type": trigger_type,
            "trigger_ref": trigger_ref,
            "dedup_key": dedup_key,
            "severity": severity,
            "status": status,
            "agent_runtime": agent_runtime,
            "agent_task_id": agent_task_id,
            "requested_workflow_type": requested_workflow_type,
            "portfolio_id": portfolio_id,
            "watchlist_id": watchlist_id,
            "recommendation_run_id": recommendation_run_id,
            "asset_id": asset_id,
            "cooldown_until": cooldown_until,
            "triggered_at": triggered_at,
            "dispatched_at": dispatched_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AssistantTriggerEventORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key != "trigger_event_id"
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssistantTriggerEventORM.trigger_event_id],
                set_=update_values,
            )
        )
        self.session.flush()
        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        self.session.refresh(event)
        return event

    def has_recent_event(
        self,
        *,
        dedup_key: str,
        since: datetime,
        statuses: Sequence[str] = ("pending", "dispatched", "skipped"),
    ) -> bool:
        """判断冷却窗口内是否已有同类触发。"""

        statement = select(func.count()).select_from(AssistantTriggerEventORM).where(
            AssistantTriggerEventORM.dedup_key == dedup_key,
            AssistantTriggerEventORM.triggered_at >= since,
            AssistantTriggerEventORM.status.in_(statuses),
        )
        return int(self.session.scalar(statement) or 0) > 0

    def list_pending_events(
        self,
        *,
        owner_id: str | None = None,
        limit: int = 50,
    ) -> list[AssistantTriggerEventORM]:
        """查询待派发触发事件。"""

        statement = select(AssistantTriggerEventORM).where(
            AssistantTriggerEventORM.status == "pending"
        )
        if owner_id:
            statement = statement.where(AssistantTriggerEventORM.owner_id == owner_id)
        return list(
            self.session.scalars(
                statement.order_by(
                    AssistantTriggerEventORM.triggered_at.asc(),
                    AssistantTriggerEventORM.severity.desc(),
                ).limit(limit)
            )
        )

    def mark_dispatched(
        self,
        *,
        trigger_event_id: str,
        agent_task_id: str,
        dispatched_at: datetime,
        payload: JsonDict | None = None,
    ) -> AssistantTriggerEventORM:
        """标记触发事件已经派发到 Agent 唤醒队列。"""

        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        merged_payload = dict(event.payload or {})
        merged_payload.update(_json_safe(payload or {}))
        return self.upsert_trigger_event(
            trigger_event_id=event.trigger_event_id,
            owner_id=event.owner_id,
            trigger_type=event.trigger_type,
            trigger_ref=event.trigger_ref,
            dedup_key=event.dedup_key,
            severity=event.severity,
            status="dispatched",
            agent_runtime=event.agent_runtime,
            agent_task_id=agent_task_id,
            requested_workflow_type=event.requested_workflow_type,
            portfolio_id=event.portfolio_id,
            watchlist_id=event.watchlist_id,
            recommendation_run_id=event.recommendation_run_id,
            asset_id=event.asset_id,
            cooldown_until=event.cooldown_until,
            triggered_at=event.triggered_at,
            dispatched_at=dispatched_at,
            payload=merged_payload,
        )

    def mark_skipped(
        self,
        *,
        trigger_event_id: str,
        skipped_at: datetime,
        reason: str,
    ) -> AssistantTriggerEventORM:
        """标记触发事件被跳过。"""

        event = self.session.get_one(AssistantTriggerEventORM, trigger_event_id)
        payload = dict(event.payload or {})
        payload["skip_reason"] = reason
        return self.upsert_trigger_event(
            trigger_event_id=event.trigger_event_id,
            owner_id=event.owner_id,
            trigger_type=event.trigger_type,
            trigger_ref=event.trigger_ref,
            dedup_key=event.dedup_key,
            severity=event.severity,
            status="skipped",
            agent_runtime=event.agent_runtime,
            agent_task_id=event.agent_task_id,
            requested_workflow_type=event.requested_workflow_type,
            portfolio_id=event.portfolio_id,
            watchlist_id=event.watchlist_id,
            recommendation_run_id=event.recommendation_run_id,
            asset_id=event.asset_id,
            cooldown_until=event.cooldown_until,
            triggered_at=event.triggered_at,
            dispatched_at=skipped_at,
            payload=payload,
        )


class DecisionLogRepository:
    """提醒和决策日志仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_monitoring_alert(
        self,
        *,
        alert_id: str,
        owner_id: str,
        alert_type: str,
        severity: str,
        triggered_by: str,
        trigger_condition: str,
        status: str,
        as_of: datetime,
        portfolio_id: str | None = None,
        asset_id: str | None = None,
        current_value: Decimal | None = None,
        threshold_value: Decimal | None = None,
        payload: JsonDict | None = None,
    ) -> MonitoringAlertORM:
        """写入或覆盖一条监控提醒。"""

        values = {
            "alert_id": alert_id,
            "owner_id": owner_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "alert_type": alert_type,
            "severity": severity,
            "triggered_by": triggered_by,
            "trigger_condition": trigger_condition,
            "current_value": current_value,
            "threshold_value": threshold_value,
            "status": status,
            "as_of": as_of,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(MonitoringAlertORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "alert_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[MonitoringAlertORM.alert_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(MonitoringAlertORM, alert_id)

    def insert_decision_log(
        self,
        *,
        decision_id: str,
        owner_id: str,
        decision_type: str,
        suggested_action: str,
        user_action: str,
        summary: str,
        portfolio_id: str | None = None,
        asset_id: str | None = None,
        source_recommendation_id: str | None = None,
        source_alert_id: str | None = None,
        workflow_run_id: str | None = None,
        reason_ids: list[str] | None = None,
        risk_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        created_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> DecisionLogORM:
        """写入或覆盖一条决策日志。"""

        values = {
            "decision_id": decision_id,
            "owner_id": owner_id,
            "portfolio_id": portfolio_id,
            "asset_id": asset_id,
            "decision_type": decision_type,
            "source_recommendation_id": source_recommendation_id,
            "source_alert_id": source_alert_id,
            "workflow_run_id": workflow_run_id,
            "suggested_action": suggested_action,
            "user_action": user_action,
            "summary": summary,
            "reason_ids": reason_ids or [],
            "risk_ids": risk_ids or [],
            "evidence_ids": evidence_ids or [],
            "created_at": created_at or datetime.now().astimezone(),
            "payload": _json_safe(payload or {}),
        }
        statement = insert(DecisionLogORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "decision_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[DecisionLogORM.decision_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(DecisionLogORM, decision_id)

    def list_recent_decisions(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 20,
    ) -> list[DecisionLogORM]:
        """查询最近决策日志。"""

        statement = select(DecisionLogORM).where(DecisionLogORM.owner_id == owner_id)
        if asset_id:
            statement = statement.where(DecisionLogORM.asset_id == asset_id)
        return list(
            self.session.scalars(
                statement.order_by(DecisionLogORM.created_at.desc()).limit(limit)
            )
        )


class MemoryRepository:
    """Finance Memory、向量索引、轻量图谱和复盘任务仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_memory(
        self,
        *,
        memory_id: str,
        owner_id: str,
        memory_type: str,
        scope: str,
        content: str,
        confidence: Decimal,
        asset_id: str | None = None,
        source_decision_id: str | None = None,
        source_review_task_id: str | None = None,
        embedding_ref: str | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> AssistantMemoryORM:
        """按 `memory_id` 幂等写入 Finance Memory。"""

        values = {
            "memory_id": memory_id,
            "owner_id": owner_id,
            "memory_type": memory_type,
            "scope": scope,
            "asset_id": asset_id,
            "source_decision_id": source_decision_id,
            "source_review_task_id": source_review_task_id,
            "content": content,
            "embedding_ref": embedding_ref,
            "confidence": confidence,
            "status": status,
            "payload": _json_safe(payload or {}),
            "updated_at": datetime.now().astimezone(),
        }
        statement = insert(AssistantMemoryORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "memory_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AssistantMemoryORM.memory_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AssistantMemoryORM, memory_id)

    def upsert_embedding(
        self,
        *,
        embedding_id: str,
        owner_id: str,
        source_type: str,
        source_id: str,
        chunk_text: str,
        embedding_model: str,
        memory_id: str | None = None,
        embedding: list[float] | None = None,
        content_hash: str | None = None,
        payload: JsonDict | None = None,
    ) -> MemoryEmbeddingORM:
        """按 `embedding_id` 幂等写入 Finance Memory 语义索引。"""

        values = {
            "embedding_id": embedding_id,
            "owner_id": owner_id,
            "memory_id": memory_id,
            "source_type": source_type,
            "source_id": source_id,
            "chunk_text": chunk_text,
            "embedding": _json_safe(embedding) if embedding is not None else None,
            "embedding_model": embedding_model,
            "content_hash": content_hash or _stable_json_hash({"text": chunk_text}),
            "payload": _json_safe(payload or {}),
        }
        statement = insert(MemoryEmbeddingORM).values(**values)
        update_values = {key: statement.excluded[key] for key in values if key != "embedding_id"}
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[MemoryEmbeddingORM.embedding_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(MemoryEmbeddingORM, embedding_id)

    def upsert_edge(
        self,
        *,
        edge_id: str,
        owner_id: str,
        source_type: str,
        source_id: str,
        relation_type: str,
        target_type: str,
        target_id: str,
        confidence: Decimal,
        reason: str | None = None,
        payload: JsonDict | None = None,
    ) -> FinancialMemoryEdgeORM:
        """按语义边唯一约束幂等写入轻量图谱关系。"""

        values = {
            "edge_id": edge_id,
            "owner_id": owner_id,
            "source_type": source_type,
            "source_id": source_id,
            "relation_type": relation_type,
            "target_type": target_type,
            "target_id": target_id,
            "confidence": confidence,
            "reason": reason,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(FinancialMemoryEdgeORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key
            not in {
                "edge_id",
                "owner_id",
                "source_type",
                "source_id",
                "relation_type",
                "target_type",
                "target_id",
            }
        }
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_memory_edges_owner_source_relation_target",
                set_=update_values,
            )
        )
        self.session.flush()
        return self.get_edge(
            owner_id=owner_id,
            source_type=source_type,
            source_id=source_id,
            relation_type=relation_type,
            target_type=target_type,
            target_id=target_id,
        )

    def upsert_review_task(
        self,
        *,
        review_task_id: str,
        owner_id: str,
        review_type: str,
        due_at: datetime,
        status: str,
        asset_id: str | None = None,
        source_decision_id: str | None = None,
        review_questions: list[JsonDict] | None = None,
        result_summary: str | None = None,
        finished_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> ReviewTaskORM:
        """按 `review_task_id` 幂等写入复盘任务。"""

        values = {
            "review_task_id": review_task_id,
            "owner_id": owner_id,
            "asset_id": asset_id,
            "source_decision_id": source_decision_id,
            "review_type": review_type,
            "due_at": due_at,
            "status": status,
            "review_questions": _json_safe(review_questions or []),
            "result_summary": result_summary,
            "finished_at": finished_at,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(ReviewTaskORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key != "review_task_id"
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[ReviewTaskORM.review_task_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(ReviewTaskORM, review_task_id)

    def get_edge(
        self,
        *,
        owner_id: str,
        source_type: str,
        source_id: str,
        relation_type: str,
        target_type: str,
        target_id: str,
    ) -> FinancialMemoryEdgeORM:
        """根据语义边唯一键查询关系。"""

        statement = select(FinancialMemoryEdgeORM).where(
            FinancialMemoryEdgeORM.owner_id == owner_id,
            FinancialMemoryEdgeORM.source_type == source_type,
            FinancialMemoryEdgeORM.source_id == source_id,
            FinancialMemoryEdgeORM.relation_type == relation_type,
            FinancialMemoryEdgeORM.target_type == target_type,
            FinancialMemoryEdgeORM.target_id == target_id,
        )
        return self.session.scalars(statement).one()

    def list_active_memories(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> list[AssistantMemoryORM]:
        """查询可用 Finance Memory。"""

        statement = select(AssistantMemoryORM).where(
            AssistantMemoryORM.owner_id == owner_id,
            AssistantMemoryORM.status == "active",
        )
        if asset_id:
            statement = statement.where(AssistantMemoryORM.asset_id == asset_id)
        if memory_type:
            statement = statement.where(AssistantMemoryORM.memory_type == memory_type)
        return list(
            self.session.scalars(
                statement.order_by(AssistantMemoryORM.updated_at.desc()).limit(limit)
            )
        )


class DataQualityRepository:
    """数据质量快照仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_quality_snapshot(
        self,
        *,
        quality_id: str,
        market: str,
        data_domain: str,
        provider: str,
        status: str,
        freshness_status: str,
        checked_at: datetime,
        issue_count: int,
        asset_id: str | None = None,
        symbol: str | None = None,
        latest_data_at: datetime | None = None,
        missing_items: list[str] | None = None,
        payload: JsonDict | None = None,
    ) -> DataQualitySnapshotORM:
        """按 `quality_id` 幂等写入数据质量快照。"""

        values = {
            "quality_id": quality_id,
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "data_domain": data_domain,
            "provider": provider,
            "status": status,
            "freshness_status": freshness_status,
            "latest_data_at": latest_data_at,
            "checked_at": checked_at,
            "missing_items": missing_items or [],
            "issue_count": issue_count,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(DataQualitySnapshotORM).values(**values)
        update_values = {
            key: statement.excluded[key]
            for key in values
            if key not in {"quality_id", "checked_at"}
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    DataQualitySnapshotORM.quality_id,
                    DataQualitySnapshotORM.checked_at,
                ],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(
            DataQualitySnapshotORM,
            {"quality_id": quality_id, "checked_at": checked_at},
        )

    def list_latest_quality(
        self,
        *,
        asset_id: str | None = None,
        market: str | None = None,
        data_domain: str | None = None,
        limit: int = 20,
    ) -> list[DataQualitySnapshotORM]:
        """查询最近数据质量快照。"""

        statement = select(DataQualitySnapshotORM)
        if asset_id:
            statement = statement.where(DataQualitySnapshotORM.asset_id == asset_id)
        if market:
            statement = statement.where(DataQualitySnapshotORM.market == market)
        if data_domain:
            statement = statement.where(DataQualitySnapshotORM.data_domain == data_domain)
        return list(
            self.session.scalars(
                statement.order_by(DataQualitySnapshotORM.checked_at.desc()).limit(limit)
            )
        )

    def list_recent_quality_since(
        self,
        *,
        asset_ids: Sequence[str],
        since: datetime,
        limit: int = 50,
    ) -> list[DataQualitySnapshotORM]:
        """查询一组资产在时间窗口内新增的数据质量快照。"""

        if not asset_ids:
            return []
        statement = (
            select(DataQualitySnapshotORM)
            .where(
                DataQualitySnapshotORM.asset_id.in_(asset_ids),
                DataQualitySnapshotORM.checked_at >= since,
            )
            .order_by(DataQualitySnapshotORM.checked_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


class WorkflowAuditRepository:
    """上层主 Agent 调用底层金融团队 Workflow 的审计仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_run(
        self,
        *,
        workflow_run_id: str,
        owner_id: str,
        workflow_type: str,
        trigger_type: str,
        status: str,
        started_at: datetime,
        trigger_ref: str | None = None,
        finished_at: datetime | None = None,
        input_ref: str | None = None,
        output_ref: str | None = None,
        payload: JsonDict | None = None,
    ) -> AgentWorkflowRunORM:
        """按 `workflow_run_id` 幂等写入 Workflow 运行审计。"""

        values = {
            "workflow_run_id": workflow_run_id,
            "owner_id": owner_id,
            "workflow_type": workflow_type,
            "trigger_type": trigger_type,
            "trigger_ref": trigger_ref,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "input_ref": input_ref,
            "output_ref": output_ref,
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AgentWorkflowRunORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key != "workflow_run_id"
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AgentWorkflowRunORM.workflow_run_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AgentWorkflowRunORM, workflow_run_id)

    def insert_event(
        self,
        *,
        workflow_event_id: str,
        workflow_run_id: str,
        event_type: str,
        message: str,
        agent_name: str | None = None,
        evidence_ids: list[str] | None = None,
        created_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> AgentWorkflowEventORM:
        """写入或覆盖一条 Workflow 可审计事件。"""

        values = {
            "workflow_event_id": workflow_event_id,
            "workflow_run_id": workflow_run_id,
            "event_type": event_type,
            "agent_name": agent_name,
            "message": message,
            "evidence_ids": evidence_ids or [],
            "created_at": created_at or datetime.now().astimezone(),
            "payload": _json_safe(payload or {}),
        }
        statement = insert(AgentWorkflowEventORM).values(**values)
        update_values = {
            key: statement.excluded[key] for key in values if key != "workflow_event_id"
        }
        self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AgentWorkflowEventORM.workflow_event_id],
                set_=update_values,
            )
        )
        self.session.flush()
        return self.session.get_one(AgentWorkflowEventORM, workflow_event_id)

    def list_events(self, workflow_run_id: str) -> list[AgentWorkflowEventORM]:
        """查询一次 Workflow 的事件。"""

        statement = (
            select(AgentWorkflowEventORM)
            .where(AgentWorkflowEventORM.workflow_run_id == workflow_run_id)
            .order_by(AgentWorkflowEventORM.created_at)
        )
        return list(self.session.scalars(statement))


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

    def list_recent_snapshots(
        self,
        *,
        asset_id: str,
        limit: int,
        window: str | None = None,
        source: str | None = None,
    ) -> list[CapitalFlowSnapshotORM]:
        """查询单标的最近 N 条资金流快照，返回时间升序结果。"""

        statement = select(CapitalFlowSnapshotORM).where(
            CapitalFlowSnapshotORM.asset_id == asset_id
        )
        if window:
            statement = statement.where(CapitalFlowSnapshotORM.window == window)
        if source:
            statement = statement.where(CapitalFlowSnapshotORM.source == source)
        rows = list(
            self.session.scalars(
                statement.order_by(CapitalFlowSnapshotORM.as_of.desc()).limit(limit)
            )
        )
        return list(reversed(rows))


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

    def list_recent_risks_since(
        self,
        *,
        asset_ids: Sequence[str],
        since: datetime,
        severities: Sequence[str] = ("high", "critical"),
        limit: int = 50,
    ) -> list[RiskFindingORM]:
        """查询一组资产在时间窗口内新增的高优先级风险。"""

        if not asset_ids:
            return []
        statement = (
            select(RiskFindingORM)
            .where(
                RiskFindingORM.asset_id.in_(asset_ids),
                RiskFindingORM.as_of >= since,
                RiskFindingORM.severity.in_(severities),
            )
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
