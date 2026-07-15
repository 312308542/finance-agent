"""推荐因子计算服务。

第一版 FactorService 负责把已经落库的指标、资金流、财务估值、事件、
风险和数字货币衍生品快照合并为 `factor_frames`。它不做推荐排序，也不让
LLM 参与打分。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.data.freshness import (
    ASHARE_HISTORICAL_VALUATION_SOURCE,
    ASHARE_SPOT_VALUATION_SOURCE,
)
from finance_agent.factors.specs import (
    DEFAULT_FACTOR_SPEC,
    AshareFactorSpec,
    CryptoFactorSpec,
    FactorSpec,
)
from finance_agent.storage.orm import (
    CapitalFlowSnapshotORM,
    CryptoDerivativeSnapshotORM,
    EventRecordORM,
    FactorFrameORM,
    FundamentalSnapshotORM,
    IndicatorFrameORM,
    RiskFindingORM,
)
from finance_agent.storage.event_retention import DEFAULT_EVENT_SIGNAL_LOOKBACK_DAYS
from finance_agent.storage.repositories import (
    CapitalFlowRepository,
    DerivativeDataRepository,
    EventRepository,
    FactorFrameRepository,
    FundamentalDataRepository,
    IndicatorFrameRepository,
    RiskRepository,
)

JsonDict = dict[str, Any]
SUPPLEMENTAL_FACTOR_GROUPS = {"sector_strength", "leadership"}


@dataclass(frozen=True)
class FactorComputationResult:
    """单次因子计算结果摘要。"""

    status: str
    factor_frame_id: str
    asset_id: str
    symbol: str
    market: str
    horizon: str
    total_available_groups: int
    missing_groups: tuple[str, ...]


class FactorService:
    """合并基础数据和指标结果，生成推荐因子快照。"""

    def __init__(self, session: Session, *, spec: FactorSpec = DEFAULT_FACTOR_SPEC) -> None:
        self.spec = spec
        self.indicators = IndicatorFrameRepository(session)
        self.factors = FactorFrameRepository(session)
        self.fundamentals = FundamentalDataRepository(session)
        self.capital_flows = CapitalFlowRepository(session)
        self.derivatives = DerivativeDataRepository(session)
        self.events = EventRepository(session)
        self.risks = RiskRepository(session)

    def compute_for_asset(
        self,
        *,
        asset_id: str,
        timeframe: str = "1d",
        horizon: str = "swing",
        indicator_library: str = "talib",
        fallback_symbol: str | None = None,
        fallback_market: str | None = None,
        supplemental_factor_groups: Iterable[Mapping[str, Any]] | None = None,
    ) -> FactorComputationResult:
        """计算单标的第一版因子快照。"""

        indicator = self.indicators.get_latest_indicator_frame(
            asset_id=asset_id,
            timeframe=timeframe,
            horizon=horizon,
            library=indicator_library,
        )
        financial_indicator_history = self.fundamentals.list_recent_snapshots(
            asset_id=asset_id,
            limit=self.spec.ashare.valuation_history_limit,
            source="akshare:stock_financial_analysis_indicator_em",
        )
        historical_valuation_history = self.fundamentals.list_recent_snapshots(
            asset_id=asset_id,
            limit=self.spec.ashare.valuation_history_limit,
            source=ASHARE_HISTORICAL_VALUATION_SOURCE,
        )
        spot_valuation_history = self.fundamentals.list_recent_snapshots(
            asset_id=asset_id,
            limit=self.spec.ashare.valuation_history_limit,
            source=ASHARE_SPOT_VALUATION_SOURCE,
        )
        valuation_history = sorted(
            historical_valuation_history + spot_valuation_history,
            key=lambda snapshot: snapshot.as_of,
        )[-self.spec.ashare.valuation_history_limit :]
        capital_flow_history = self.capital_flows.list_recent_snapshots(
            asset_id=asset_id,
            limit=self.spec.ashare.capital_flow_history_limit,
        )
        derivative_history = self.derivatives.list_recent_snapshots(
            asset_id=asset_id,
            limit=self.spec.crypto.derivative_history_limit,
        )
        fundamental_history = financial_indicator_history + valuation_history
        fundamental = financial_indicator_history[-1] if financial_indicator_history else None
        valuation = valuation_history[-1] if valuation_history else None
        capital_flow = capital_flow_history[-1] if capital_flow_history else None
        derivative = derivative_history[-1] if derivative_history else None
        events = self.events.list_recent_events(
            asset_id=asset_id,
            limit=20,
            max_age_days=DEFAULT_EVENT_SIGNAL_LOOKBACK_DAYS,
        )
        risks = self.risks.list_recent_risks(asset_id=asset_id, limit=20)

        symbol, market = infer_symbol_market(
            indicator=indicator,
            fundamental=fundamental,
            capital_flow=capital_flow,
            derivative=derivative,
            fallback_symbol=fallback_symbol,
            fallback_market=fallback_market,
        )
        as_of = indicator.input_end_at if indicator else datetime.now(tz=UTC)
        supplemental_groups = normalize_supplemental_factor_groups(
            supplemental_factor_groups or []
        )
        groups = [
            build_technical_group(indicator),
            build_fundamental_group(fundamental),
            build_valuation_group(
                valuation,
                history=valuation_history,
                spec=self.spec.ashare,
            ),
            build_capital_flow_group(
                capital_flow,
                history=capital_flow_history,
                spec=self.spec.ashare,
            ),
            build_liquidity_group(
                indicator,
                capital_flow=capital_flow,
                spec=self.spec.ashare,
            ),
            build_derivatives_group(
                derivative,
                history=derivative_history,
                spec=self.spec.crypto,
            ),
            build_event_group(events),
            build_event_decay_group(events),
            build_risk_group(risks),
        ]
        if market.startswith("crypto"):
            groups = [
                group
                for group in groups
                if group["group"] not in {"fundamental", "valuation", "capital_flow"}
            ]
        elif market.startswith("ashare"):
            groups = [group for group in groups if group["group"] != "derivatives"]
            groups = append_supplemental_factor_groups(groups, supplemental_groups)
        missing_groups = [item["group"] for item in groups if item["status"] == "unavailable"]
        partial_groups = [item["group"] for item in groups if item["status"] == "partial"]
        source_ids = collect_source_ids(
            indicator=indicator,
            fundamental=fundamental,
            capital_flow=capital_flow,
            derivative=derivative,
            fundamental_history=fundamental_history,
            capital_flow_history=capital_flow_history,
            derivative_history=derivative_history,
            events=events,
            risks=risks,
            supplemental_factor_groups=supplemental_groups,
        )
        available_count = len(groups) - len(missing_groups)
        status = "available" if not missing_groups and not partial_groups else "partial"
        if available_count == 0:
            status = "unavailable"

        factor_frame_id = build_factor_frame_id(asset_id=asset_id, horizon=horizon, as_of=as_of)
        saved = self.factors.upsert_factor_frame(
            factor_frame_id=factor_frame_id,
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            horizon=horizon,
            status=status,
            total_available_groups=available_count,
            missing_groups=missing_groups,
            source_ids=source_ids,
            indicator_frame_id=indicator.indicator_frame_id if indicator else None,
            as_of=as_of,
            payload={
                "schema_version": "1.0",
                "timeframe": timeframe,
                "factor_groups": groups,
                "partial_groups": partial_groups,
                "model_policy": {
                    "scoring": "deterministic_rules",
                    "llm_role": "explanation_only",
                    "quant_models": "todo_after_recommendation_loop",
                },
            },
        )
        return result_from_frame(saved)


def build_technical_group(indicator: IndicatorFrameORM | None) -> JsonDict:
    """构建技术面因子组。"""

    if indicator is None:
        return unavailable_group("technical", ["indicator_frame"])

    values = dict(indicator.payload.get("computed_values") or {})
    factors = {
        "return_1d": values.get("return_1d"),
        "return_5d": values.get("return_5d"),
        "return_20d": values.get("return_20d"),
        "momentum_20d": values.get("momentum_20d"),
        "ma_20": decimal_to_float(indicator.ma_20),
        "ma_60": decimal_to_float(indicator.ma_60),
        "ma_slope": values.get("ma_slope"),
        "rsi_14": decimal_to_float(indicator.rsi_14),
        "macd": decimal_to_float(indicator.macd),
        "macd_hist": decimal_to_float(indicator.macd_hist),
        "atr_14": decimal_to_float(indicator.atr_14),
        "bb_percent_b": decimal_to_float(indicator.bb_percent_b),
        "volatility_20d": values.get("volatility_20d"),
        "max_drawdown_20d": values.get("max_drawdown_20d"),
    }
    missing = [key for key, value in factors.items() if value is None]
    return {
        "group": "technical",
        "status": "available" if not missing else "partial",
        "score": technical_score(factors),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": [indicator.indicator_frame_id],
    }


def build_fundamental_group(snapshot: FundamentalSnapshotORM | None) -> JsonDict:
    """构建基本面因子组。"""

    if snapshot is None:
        return unavailable_group("fundamental", ["fundamental_snapshot"])

    factors = {
        "roe_score": positive_score(snapshot.roe, scale=Decimal("0.20")),
        "revenue_growth_score": signed_score(snapshot.revenue_growth_yoy, scale=Decimal("0.30")),
        "profit_growth_score": signed_score(
            snapshot.net_profit_growth_yoy,
            scale=Decimal("0.30"),
        ),
        "cashflow_quality": presence_score(snapshot.operating_cashflow),
        "debt_risk_score": inverse_score(snapshot.debt_to_asset, scale=Decimal("0.80")),
    }
    missing = [key for key, value in factors.items() if value is None]
    return {
        "group": "fundamental",
        "status": "available" if not missing else "partial",
        "score": average_score(factors),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": [snapshot.snapshot_id],
    }


def build_valuation_group(
    snapshot: FundamentalSnapshotORM | None,
    *,
    history: list[FundamentalSnapshotORM] | None = None,
    spec: AshareFactorSpec = DEFAULT_FACTOR_SPEC.ashare,
) -> JsonDict:
    """构建估值因子组。"""

    if snapshot is None:
        return unavailable_group("valuation", ["fundamental_snapshot"])

    history = history or [snapshot]
    pe_percentile = history_percentile(
        snapshot.pe_ttm,
        [item.pe_ttm for item in history],
        min_observations=spec.valuation_percentile_min_observations,
    )
    pb_percentile = history_percentile(
        snapshot.pb,
        [item.pb for item in history],
        min_observations=spec.valuation_percentile_min_observations,
    )
    dividend_yield = payload_float(snapshot.payload, "dividend_yield", "dividend_yield_ttm")
    dividend_score = positive_float_score(dividend_yield, scale=spec.dividend_full_score_yield)
    factors = {
        "pe_ttm": decimal_to_float(snapshot.pe_ttm),
        "pb": decimal_to_float(snapshot.pb),
        "pe_percentile": pe_percentile,
        "pb_percentile": pb_percentile,
        "dividend_yield": dividend_yield,
        "dividend_score": dividend_score,
        "valuation_overheat": valuation_overheat(snapshot, pe_percentile=pe_percentile),
    }
    missing = [key for key, value in factors.items() if value is None]
    score_inputs = {
        "pe_score": inverse_score(snapshot.pe_ttm, scale=Decimal("60")),
        "pe_percentile_score": inverse_float_score(pe_percentile, scale=1),
        "pb_percentile_score": inverse_float_score(pb_percentile, scale=1),
        "dividend_score": dividend_score,
    }
    return {
        "group": "valuation",
        "status": "available" if not missing else "partial",
        "score": average_score(score_inputs),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": unique_source_ids(item.snapshot_id for item in history),
    }


def build_capital_flow_group(
    snapshot: CapitalFlowSnapshotORM | None,
    *,
    history: list[CapitalFlowSnapshotORM] | None = None,
    spec: AshareFactorSpec = DEFAULT_FACTOR_SPEC.ashare,
) -> JsonDict:
    """构建资金流因子组。"""

    if snapshot is None:
        return unavailable_group("capital_flow", ["capital_flow_snapshot"])

    history = history or [snapshot]
    strength = safe_decimal_ratio(snapshot.main_net_inflow, snapshot.amount)
    flow_rank_percentile = payload_float(
        snapshot.payload,
        "flow_rank_percentile",
        "rank_percentile",
        "percentile",
    )
    if flow_rank_percentile is None:
        flow_rank_percentile = rank_hint_percentile(snapshot.payload)
    flow_continuity = capital_flow_continuity(
        history,
        window=spec.capital_flow_continuity_window,
    )
    northbound_strength = safe_decimal_ratio(snapshot.northbound_net_inflow, snapshot.amount)
    northbound_continuity = northbound_flow_continuity(
        history,
        window=spec.capital_flow_continuity_window,
    )
    flow_price_divergence = payload_float(
        snapshot.payload,
        "flow_price_divergence",
        "price_divergence",
    )
    factors = {
        "main_net_inflow_strength": strength,
        "northbound_net_inflow_strength": northbound_strength,
        "flow_rank_percentile": flow_rank_percentile,
        "flow_continuity": flow_continuity,
        "northbound_flow_continuity": northbound_continuity,
        "flow_price_divergence": flow_price_divergence,
        "window": snapshot.window,
    }
    missing = [key for key, value in factors.items() if value is None]
    score_inputs = {
        "main_net_inflow_strength": signed_float_score(
            strength,
            scale=spec.main_flow_strength_scale,
        ),
        "northbound_net_inflow_strength": signed_float_score(
            northbound_strength,
            scale=spec.main_flow_strength_scale,
        ),
        "flow_rank_percentile": percentile_score(flow_rank_percentile),
        "flow_continuity": positive_float_score(flow_continuity, scale=1),
        "northbound_flow_continuity": positive_float_score(northbound_continuity, scale=1),
        "flow_price_divergence": signed_float_score(flow_price_divergence, scale=0.05),
    }
    return {
        "group": "capital_flow",
        "status": "available" if not missing else "partial",
        "score": average_score(score_inputs),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": unique_source_ids(item.snapshot_id for item in history),
    }


def build_liquidity_group(
    indicator: IndicatorFrameORM | None,
    *,
    capital_flow: CapitalFlowSnapshotORM | None = None,
    spec: AshareFactorSpec = DEFAULT_FACTOR_SPEC.ashare,
) -> JsonDict:
    """构建流动性因子组。"""

    if indicator is None:
        return unavailable_group("liquidity", ["indicator_frame"])

    values = dict(indicator.payload.get("computed_values") or {})
    amount_avg_20d = values.get("amount_avg_20d")
    amount_zscore_20d = values.get("amount_zscore_20d")
    volatility_20d = values.get("volatility_20d")
    max_drawdown_20d = values.get("max_drawdown_20d")
    turnover_rate = decimal_to_float(capital_flow.turnover_rate) if capital_flow else None
    illiquidity_score = normalized_illiquidity(
        amount_avg_20d=amount_avg_20d,
        amount_zscore_20d=amount_zscore_20d,
        volatility_20d=volatility_20d,
        turnover_rate=turnover_rate,
    )
    factors = {
        "amount_avg_20d": amount_avg_20d,
        "amount_zscore_20d": amount_zscore_20d,
        "volatility_20d": volatility_20d,
        "turnover_rate": turnover_rate,
        "illiquidity_score": illiquidity_score,
        "max_drawdown_20d": max_drawdown_20d,
    }
    required_factor_keys = [
        "amount_avg_20d",
        "amount_zscore_20d",
        "volatility_20d",
        "illiquidity_score",
        "max_drawdown_20d",
    ]
    if indicator.market.startswith("ashare"):
        required_factor_keys.append("turnover_rate")
    missing = [key for key in required_factor_keys if factors.get(key) is None]
    score_inputs = {
        "amount_avg_20d_score": positive_float_score(amount_avg_20d, scale=1_000_000_000),
        "amount_zscore_20d_score": inverse_float_score(
            abs(amount_zscore_20d) if amount_zscore_20d is not None else None,
            scale=3,
        ),
        "volatility_20d_score": inverse_float_score(volatility_20d, scale=0.6),
        "turnover_rate_score": positive_float_score(turnover_rate, scale=1),
        "illiquidity_risk_score": inverse_float_score(illiquidity_score, scale=1),
    }
    return {
        "group": "liquidity",
        "status": "available" if not missing else "partial",
        "score": average_score(score_inputs),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": unique_source_ids(
            filter(
                None,
                [indicator.indicator_frame_id, capital_flow.snapshot_id if capital_flow else None],
            )
        ),
    }


def build_derivatives_group(
    snapshot: CryptoDerivativeSnapshotORM | None,
    *,
    history: list[CryptoDerivativeSnapshotORM] | None = None,
    spec: CryptoFactorSpec = DEFAULT_FACTOR_SPEC.crypto,
) -> JsonDict:
    """构建数字货币衍生品因子组。"""

    if snapshot is None:
        return unavailable_group("derivatives", ["crypto_derivative_snapshot"])

    history = history or [snapshot]
    funding_rate_zscore = decimal_zscore(
        snapshot.funding_rate,
        [item.funding_rate for item in history],
        min_observations=spec.funding_zscore_min_observations,
    )
    open_interest_change = trailing_decimal_change(
        snapshot.open_interest,
        [item.open_interest for item in history],
        lag=spec.open_interest_change_lag,
    )
    open_interest_value_change = trailing_decimal_change(
        snapshot.open_interest_value,
        [item.open_interest_value for item in history],
        lag=spec.open_interest_change_lag,
    )
    factors = {
        "funding_rate": decimal_to_float(snapshot.funding_rate),
        "funding_rate_zscore": funding_rate_zscore,
        "open_interest": decimal_to_float(snapshot.open_interest),
        "open_interest_change": open_interest_change,
        "open_interest_value_change": open_interest_value_change,
        "long_short_ratio": decimal_to_float(snapshot.long_short_ratio),
        "long_short_crowding": long_short_crowding(snapshot.long_short_ratio),
        "basis_rate": decimal_to_float(snapshot.basis_rate),
    }
    missing = [key for key, value in factors.items() if value is None]
    score_inputs = {
        "funding_abs_score": inverse_score_abs(snapshot.funding_rate, scale=Decimal("0.001")),
        "funding_zscore_score": inverse_float_score(
            abs(funding_rate_zscore) if funding_rate_zscore is not None else None,
            scale=3,
        ),
        "open_interest_change_score": signed_float_score(
            open_interest_change,
            scale=spec.open_interest_positive_scale,
        ),
        "long_short_crowding_score": inverse_float_score(
            long_short_crowding(snapshot.long_short_ratio),
            scale=1,
        ),
    }
    return {
        "group": "derivatives",
        "status": "available" if not missing else "partial",
        "score": average_score(score_inputs),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": unique_source_ids(item.snapshot_id for item in history),
    }


def build_event_group(events: list[EventRecordORM]) -> JsonDict:
    """构建事件因子组。"""

    if not events:
        return unavailable_group("event", ["event_records"])

    negative_count = sum(1 for item in events if item.sentiment == "negative")
    factors = {
        "event_count": len(events),
        "negative_event_count": negative_count,
        "event_freshness": 1.0,
        "announcement_risk": negative_count,
    }
    return {
        "group": "event",
        "status": "available",
        "score": clamp_score(80 - negative_count * 10),
        "factors": factors,
        "missing_factors": [],
        "source_ids": [item.event_id for item in events],
    }


def build_event_decay_group(events: list[EventRecordORM]) -> JsonDict:
    """构建事件衰减因子组。"""

    if not events:
        return unavailable_group("event_decay", ["event_records"])

    now = datetime.now(tz=UTC)
    decay_scores: list[float] = []
    weighted_negative = 0.0
    weighted_positive = 0.0
    for item in events:
        published_at = item.published_at or item.collected_at
        if published_at is None:
            continue
        age_hours = max(0.0, (now - published_at.astimezone(UTC)).total_seconds() / 3600)
        decay = math.exp(-age_hours / 48.0)
        decay_scores.append(decay)
        if item.sentiment == "negative":
            weighted_negative += decay
        elif item.sentiment == "positive":
            weighted_positive += decay

    if not decay_scores:
        return unavailable_group("event_decay", ["event_records"])

    factors = {
        "event_decay_score": sum(decay_scores) / len(decay_scores),
        "weighted_negative_event_count": weighted_negative,
        "weighted_positive_event_count": weighted_positive,
        "recent_event_count": len(events),
    }
    return {
        "group": "event_decay",
        "status": "available",
        "score": clamp_score(
            100 * factors["event_decay_score"] - weighted_negative * 20 + weighted_positive * 5
        ),
        "factors": factors,
        "missing_factors": [],
        "source_ids": [item.event_id for item in events],
    }


def build_risk_group(risks: list[RiskFindingORM]) -> JsonDict:
    """构建风险因子组。"""

    if not risks:
        return unavailable_group("risk", ["risk_findings"])

    severity_weight = {"low": 10, "medium": 25, "high": 45, "critical": 70}
    penalty = sum(severity_weight.get(item.severity, 20) for item in risks)
    factors = {
        "risk_count": len(risks),
        "risk_penalty": min(100, penalty),
        "risk_types": sorted({item.risk_type for item in risks}),
    }
    return {
        "group": "risk",
        "status": "available",
        "score": clamp_score(100 - penalty),
        "factors": factors,
        "missing_factors": [],
        "source_ids": [item.risk_id for item in risks],
    }


def normalize_supplemental_factor_groups(
    groups: Iterable[Mapping[str, Any]],
) -> list[JsonDict]:
    """清洗外部注入的题材因子组，只允许确定性题材/龙头因子入帧。"""

    normalized: list[JsonDict] = []
    seen: set[str] = set()
    for group in groups:
        group_name = str(group.get("group") or "").strip()
        if group_name not in SUPPLEMENTAL_FACTOR_GROUPS or group_name in seen:
            continue
        seen.add(group_name)
        factors = group.get("factors")
        missing_factors = group.get("missing_factors") or []
        source_ids = supplemental_group_source_ids(group)
        normalized.append(
            {
                "group": group_name,
                "status": str(group.get("status") or "available"),
                "score": coerce_float(group.get("score")),
                "factors": dict(factors) if isinstance(factors, Mapping) else {},
                "missing_factors": [str(item) for item in missing_factors if item],
                "source_ids": source_ids,
            }
        )
    return normalized


def append_supplemental_factor_groups(
    groups: list[JsonDict],
    supplemental_groups: list[JsonDict],
) -> list[JsonDict]:
    """把题材因子组追加到基础因子组，已存在的组不重复写入。"""

    existing = {str(group.get("group")) for group in groups}
    return groups + [group for group in supplemental_groups if group["group"] not in existing]


def supplemental_group_source_ids(group: Mapping[str, Any]) -> list[str]:
    """从题材因子组中提取审计来源 ID。"""

    values: list[str] = []
    for key in ("source_ids", "evidence_ids"):
        raw_values = group.get(key) or []
        if isinstance(raw_values, str):
            values.append(raw_values)
        elif isinstance(raw_values, Iterable):
            values.extend(str(value) for value in raw_values if value)
    return unique_source_ids(values)


def unavailable_group(group: str, missing: list[str]) -> JsonDict:
    """构建不可用因子组。"""

    return {
        "group": group,
        "status": "unavailable",
        "score": None,
        "factors": {},
        "missing_factors": missing,
        "source_ids": [],
    }


def infer_symbol_market(
    *,
    indicator: IndicatorFrameORM | None,
    fundamental: FundamentalSnapshotORM | None,
    capital_flow: CapitalFlowSnapshotORM | None,
    derivative: CryptoDerivativeSnapshotORM | None,
    fallback_symbol: str | None = None,
    fallback_market: str | None = None,
) -> tuple[str, str]:
    """从可用输入中推断 symbol 和 market。"""

    symbol = fallback_symbol
    market = fallback_market
    for item in (indicator, fundamental, capital_flow, derivative):
        if item is not None:
            symbol = symbol or item.symbol
            market = market or getattr(item, "market", None)
            if symbol and market:
                return symbol, market
    return symbol or "unknown", market or "unknown"


def collect_source_ids(
    *,
    indicator: IndicatorFrameORM | None,
    fundamental: FundamentalSnapshotORM | None,
    capital_flow: CapitalFlowSnapshotORM | None,
    derivative: CryptoDerivativeSnapshotORM | None,
    events: list[EventRecordORM],
    risks: list[RiskFindingORM],
    fundamental_history: list[FundamentalSnapshotORM] | None = None,
    capital_flow_history: list[CapitalFlowSnapshotORM] | None = None,
    derivative_history: list[CryptoDerivativeSnapshotORM] | None = None,
    supplemental_factor_groups: list[JsonDict] | None = None,
) -> list[str]:
    """收集因子来源 ID。"""

    source_ids: list[str] = []
    if indicator:
        source_ids.append(indicator.indicator_frame_id)
    if fundamental:
        source_ids.append(fundamental.snapshot_id)
    if capital_flow:
        source_ids.append(capital_flow.snapshot_id)
    if derivative:
        source_ids.append(derivative.snapshot_id)
    source_ids.extend(item.snapshot_id for item in fundamental_history or [])
    source_ids.extend(item.snapshot_id for item in capital_flow_history or [])
    source_ids.extend(item.snapshot_id for item in derivative_history or [])
    source_ids.extend(item.event_id for item in events)
    source_ids.extend(item.risk_id for item in risks)
    for group in supplemental_factor_groups or []:
        source_ids.extend(group.get("source_ids") or [])
    return unique_source_ids(source_ids)


def build_factor_frame_id(*, asset_id: str, horizon: str, as_of: datetime) -> str:
    """生成稳定因子结果 ID。"""

    normalized_time = as_of.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"factor:{asset_id}:{horizon}:{normalized_time}"


def result_from_frame(frame: FactorFrameORM) -> FactorComputationResult:
    """把 ORM 转换成计算摘要。"""

    return FactorComputationResult(
        status=frame.status,
        factor_frame_id=frame.factor_frame_id,
        asset_id=frame.asset_id,
        symbol=frame.symbol,
        market=frame.market,
        horizon=frame.horizon,
        total_available_groups=frame.total_available_groups,
        missing_groups=tuple(frame.missing_groups),
    )


def technical_score(factors: JsonDict) -> float | None:
    """计算透明规则技术分。"""

    scores = [
        signed_float_score(factors.get("return_20d"), scale=0.20),
        rsi_score(factors.get("rsi_14")),
        signed_float_score(factors.get("macd_hist"), scale=500),
        inverse_float_score(abs(factors["max_drawdown_20d"]), scale=0.20)
        if factors.get("max_drawdown_20d") is not None
        else None,
    ]
    valid = [item for item in scores if item is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def derivatives_score(snapshot: CryptoDerivativeSnapshotORM) -> float | None:
    """计算衍生品拥挤度简化分。"""

    scores = [
        inverse_score_abs(snapshot.funding_rate, scale=Decimal("0.001")),
        inverse_score_abs(
            snapshot.long_short_ratio - Decimal("1")
            if snapshot.long_short_ratio is not None
            else None,
            scale=Decimal("1"),
        ),
    ]
    valid = [item for item in scores if item is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def valuation_overheat(
    snapshot: FundamentalSnapshotORM,
    *,
    pe_percentile: float | None = None,
) -> float | None:
    """粗略估值过热标记，后续会被历史分位替代。"""

    if pe_percentile is not None:
        return clamp(pe_percentile, 0, 1)
    if snapshot.pe_ttm is None:
        return None
    return clamp(decimal_to_float(snapshot.pe_ttm / Decimal("80")) or 0, 0, 1)


def long_short_crowding(value: Decimal | None) -> float | None:
    """多空比拥挤度。"""

    if value is None:
        return None
    return clamp(decimal_to_float(abs(value - Decimal("1"))) or 0, 0, 1)


def normalized_illiquidity(
    *,
    amount_avg_20d: float | None,
    amount_zscore_20d: float | None,
    volatility_20d: float | None,
    turnover_rate: float | None,
) -> float | None:
    """把流动性要素压成 0-1 的非流动性风险分数。"""

    components: list[float] = []
    if amount_avg_20d is not None:
        amount_score = inverse_float_score(amount_avg_20d, scale=1_000_000_000)
        components.append((amount_score or 0) / 100)
    if amount_zscore_20d is not None:
        components.append(clamp(abs(amount_zscore_20d) / 5, 0, 1))
    if volatility_20d is not None:
        components.append(clamp(volatility_20d / 1, 0, 1))
    if turnover_rate is not None:
        turnover_score = inverse_float_score(turnover_rate, scale=1)
        components.append((turnover_score or 0) / 100)
    if not components:
        return None
    return clamp(sum(components) / len(components), 0, 1)


def history_percentile(
    value: Decimal | None,
    history: list[Decimal | None],
    *,
    min_observations: int,
) -> float | None:
    """计算当前值在历史窗口中的百分位。"""

    current = decimal_to_float(value)
    values = [decimal_to_float(item) for item in history]
    clean_values = sorted(item for item in values if item is not None and math.isfinite(item))
    if current is None or len(clean_values) < min_observations:
        return None
    lower_or_equal = sum(1 for item in clean_values if item <= current)
    return clamp(lower_or_equal / len(clean_values), 0, 1)


def capital_flow_continuity(
    history: list[CapitalFlowSnapshotORM],
    *,
    window: int,
) -> float | None:
    """计算最近窗口内主力资金净流入为正的占比。"""

    recent = [
        item.main_net_inflow
        for item in history[-window:]
        if item.main_net_inflow is not None
    ]
    if not recent:
        return None
    positive_count = sum(1 for item in recent if item > 0)
    return clamp(positive_count / len(recent), 0, 1)


def northbound_flow_continuity(
    history: list[CapitalFlowSnapshotORM],
    *,
    window: int,
) -> float | None:
    """计算最近窗口内北向净流入为正的占比。"""

    recent = [
        item.northbound_net_inflow
        for item in history[-window:]
        if item.northbound_net_inflow is not None
    ]
    if not recent:
        return None
    positive_count = sum(1 for item in recent if item > 0)
    return clamp(positive_count / len(recent), 0, 1)


def decimal_zscore(
    value: Decimal | None,
    history: list[Decimal | None],
    *,
    min_observations: int,
) -> float | None:
    """计算 Decimal 序列的标准分。"""

    current = decimal_to_float(value)
    values = [decimal_to_float(item) for item in history]
    clean_values = [item for item in values if item is not None and math.isfinite(item)]
    if current is None or len(clean_values) < min_observations:
        return None
    mean = sum(clean_values) / len(clean_values)
    variance = sum((item - mean) ** 2 for item in clean_values) / len(clean_values)
    stddev = math.sqrt(variance)
    if stddev == 0:
        return 0.0
    return (current - mean) / stddev


def trailing_decimal_change(
    value: Decimal | None,
    history: list[Decimal | None],
    *,
    lag: int,
) -> float | None:
    """计算相对 lag 窗口前的变化率。"""

    current = decimal_to_float(value)
    clean_values = [decimal_to_float(item) for item in history]
    clean_values = [item for item in clean_values if item is not None and math.isfinite(item)]
    if current is None or len(clean_values) < 2:
        return None
    base_index = max(0, len(clean_values) - lag - 1)
    base = clean_values[base_index]
    if base == 0:
        return None
    return (current - base) / abs(base)


def payload_float(payload: JsonDict | None, *keys: str) -> float | None:
    """从 payload 的顶层或 raw 字段中读取浮点数。"""

    if not payload:
        return None
    for key in keys:
        parsed = coerce_float(payload.get(key))
        if parsed is not None:
            return parsed
    raw = payload.get("raw")
    if isinstance(raw, dict):
        for key in keys:
            parsed = coerce_float(raw.get(key))
            if parsed is not None:
                return parsed
    return None


def rank_hint_percentile(payload: JsonDict | None) -> float | None:
    """把 AKShare 排名提示转换成 0-1 分位，越靠前越接近 1。"""

    if not payload:
        return None
    rank = coerce_float(payload.get("rank_hint"))
    total = coerce_float(payload.get("rank_total"))
    raw = payload.get("raw")
    if rank is None and isinstance(raw, dict):
        rank = coerce_float(raw.get("rank_hint"))
    if total is None and isinstance(raw, dict):
        total = coerce_float(raw.get("rank_total") or raw.get("总数"))
    if rank is None or rank <= 0:
        return None
    if total is None or total < rank:
        return None
    if total <= 1:
        return 1.0
    return clamp(1 - ((rank - 1) / (total - 1)), 0, 1)


def percentile_score(value: float | None) -> float | None:
    """把 0-1 分位转换为 0-100 得分。"""

    return positive_float_score(value, scale=1)


def positive_float_score(value: float | None, *, scale: float) -> float | None:
    """浮点正向指标得分。"""

    if value is None or scale == 0:
        return None
    return clamp_score(value / scale * 100)


def coerce_float(value: Any) -> float | None:
    """把常见数值表达转换为 float。"""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return decimal_to_float(value)
    if isinstance(value, int | float):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        multiplier = 1.0
        if text.endswith("%"):
            multiplier = 0.01
            text = text[:-1]
        try:
            parsed = float(text) * multiplier
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def unique_source_ids(values: Iterable[str]) -> list[str]:
    """保持顺序去重来源 ID。"""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def positive_score(value: Decimal | None, *, scale: Decimal) -> float | None:
    """正向指标得分。"""

    if value is None or scale == 0:
        return None
    return clamp_score(decimal_to_float(value / scale) * 100)


def signed_score(value: Decimal | None, *, scale: Decimal) -> float | None:
    """正负向指标得分，0 映射到 50。"""

    if value is None or scale == 0:
        return None
    return clamp_score(50 + decimal_to_float(value / scale) * 50)


def signed_float_score(value: float | None, *, scale: float) -> float | None:
    """浮点正负向指标得分。"""

    if value is None or scale == 0:
        return None
    return clamp_score(50 + value / scale * 50)


def inverse_score(value: Decimal | None, *, scale: Decimal) -> float | None:
    """越小越好的指标得分。"""

    if value is None or scale == 0:
        return None
    return clamp_score(100 - decimal_to_float(value / scale) * 100)


def inverse_score_abs(value: Decimal | None, *, scale: Decimal) -> float | None:
    """绝对偏离越小越好的指标得分。"""

    if value is None or scale == 0:
        return None
    return clamp_score(100 - decimal_to_float(abs(value) / scale) * 100)


def inverse_float_score(value: float | None, *, scale: float) -> float | None:
    """浮点越小越好的指标得分。"""

    if value is None or scale == 0:
        return None
    return clamp_score(100 - value / scale * 100)


def rsi_score(value: float | None) -> float | None:
    """RSI 中性区间得分。"""

    if value is None:
        return None
    if 45 <= value <= 65:
        return 80.0
    if 35 <= value < 45 or 65 < value <= 75:
        return 60.0
    return 35.0


def presence_score(value: Decimal | None) -> float | None:
    """字段存在性质量分。"""

    return 60.0 if value is not None else None


def average_score(factors: JsonDict) -> float | None:
    """对因子组内已有分数取平均。"""

    valid = [
        float(value)
        for value in factors.values()
        if isinstance(value, int | float) and math.isfinite(float(value))
    ]
    if not valid:
        return None
    return sum(valid) / len(valid)


def safe_decimal_ratio(numerator: Decimal | None, denominator: Decimal | None) -> float | None:
    """Decimal 安全除法。"""

    if numerator is None or denominator in {None, Decimal("0")}:
        return None
    return decimal_to_float(numerator / denominator)


def decimal_to_float(value: Decimal | None) -> float | None:
    """Decimal 转浮点。"""

    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def clamp_score(value: float) -> float:
    """裁剪到 0-100 分。"""

    return clamp(float(value), 0, 100)


def clamp(value: float, low: float, high: float) -> float:
    """裁剪数值区间。"""

    return max(low, min(high, value))
