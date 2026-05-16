"""推荐因子计算服务。

第一版 FactorService 负责把已经落库的指标、资金流、财务估值、事件、
风险和数字货币衍生品快照合并为 `factor_frames`。它不做推荐排序，也不让
LLM 参与打分。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    CapitalFlowSnapshotORM,
    CryptoDerivativeSnapshotORM,
    EventRecordORM,
    FactorFrameORM,
    FundamentalSnapshotORM,
    IndicatorFrameORM,
    RiskFindingORM,
)
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

    def __init__(self, session: Session) -> None:
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
    ) -> FactorComputationResult:
        """计算单标的第一版因子快照。"""

        indicator = self.indicators.get_latest_indicator_frame(
            asset_id=asset_id,
            timeframe=timeframe,
            horizon=horizon,
            library=indicator_library,
        )
        fundamental = self.fundamentals.get_latest_snapshot(asset_id=asset_id)
        capital_flow = self.capital_flows.get_latest_snapshot(asset_id=asset_id)
        derivative = self.derivatives.get_latest_snapshot(asset_id=asset_id)
        events = self.events.list_recent_events(asset_id=asset_id, limit=20)
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
        groups = [
            build_technical_group(indicator),
            build_fundamental_group(fundamental),
            build_valuation_group(fundamental),
            build_capital_flow_group(capital_flow),
            build_derivatives_group(derivative),
            build_event_group(events),
            build_risk_group(risks),
        ]
        missing_groups = [item["group"] for item in groups if item["status"] == "unavailable"]
        partial_groups = [item["group"] for item in groups if item["status"] == "partial"]
        source_ids = collect_source_ids(
            indicator=indicator,
            fundamental=fundamental,
            capital_flow=capital_flow,
            derivative=derivative,
            events=events,
            risks=risks,
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


def build_valuation_group(snapshot: FundamentalSnapshotORM | None) -> JsonDict:
    """构建估值因子组。"""

    if snapshot is None:
        return unavailable_group("valuation", ["fundamental_snapshot"])

    factors = {
        "pe_ttm": decimal_to_float(snapshot.pe_ttm),
        "pb": decimal_to_float(snapshot.pb),
        "pe_percentile": None,
        "pb_percentile": None,
        "dividend_score": None,
        "valuation_overheat": valuation_overheat(snapshot),
    }
    missing = [key for key, value in factors.items() if value is None]
    return {
        "group": "valuation",
        "status": "available" if not missing else "partial",
        "score": inverse_score(snapshot.pe_ttm, scale=Decimal("60")),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": [snapshot.snapshot_id],
    }


def build_capital_flow_group(snapshot: CapitalFlowSnapshotORM | None) -> JsonDict:
    """构建资金流因子组。"""

    if snapshot is None:
        return unavailable_group("capital_flow", ["capital_flow_snapshot"])

    strength = safe_decimal_ratio(snapshot.main_net_inflow, snapshot.amount)
    factors = {
        "main_net_inflow_strength": strength,
        "flow_rank_percentile": None,
        "flow_continuity": None,
        "flow_price_divergence": None,
        "window": snapshot.window,
    }
    missing = [key for key, value in factors.items() if value is None]
    return {
        "group": "capital_flow",
        "status": "available" if not missing else "partial",
        "score": signed_float_score(strength, scale=0.10),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": [snapshot.snapshot_id],
    }


def build_derivatives_group(snapshot: CryptoDerivativeSnapshotORM | None) -> JsonDict:
    """构建数字货币衍生品因子组。"""

    if snapshot is None:
        return unavailable_group("derivatives", ["crypto_derivative_snapshot"])

    factors = {
        "funding_rate": decimal_to_float(snapshot.funding_rate),
        "funding_rate_zscore": None,
        "open_interest": decimal_to_float(snapshot.open_interest),
        "open_interest_change": None,
        "long_short_ratio": decimal_to_float(snapshot.long_short_ratio),
        "long_short_crowding": long_short_crowding(snapshot.long_short_ratio),
        "basis_rate": decimal_to_float(snapshot.basis_rate),
    }
    missing = [key for key, value in factors.items() if value is None]
    return {
        "group": "derivatives",
        "status": "available" if not missing else "partial",
        "score": derivatives_score(snapshot),
        "factors": factors,
        "missing_factors": missing,
        "source_ids": [snapshot.snapshot_id],
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

    for item in (indicator, fundamental, capital_flow, derivative):
        if item is not None:
            return item.symbol, item.market
    return fallback_symbol or "unknown", fallback_market or "unknown"


def collect_source_ids(
    *,
    indicator: IndicatorFrameORM | None,
    fundamental: FundamentalSnapshotORM | None,
    capital_flow: CapitalFlowSnapshotORM | None,
    derivative: CryptoDerivativeSnapshotORM | None,
    events: list[EventRecordORM],
    risks: list[RiskFindingORM],
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
    source_ids.extend(item.event_id for item in events)
    source_ids.extend(item.risk_id for item in risks)
    return source_ids


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


def valuation_overheat(snapshot: FundamentalSnapshotORM) -> float | None:
    """粗略估值过热标记，后续会被历史分位替代。"""

    if snapshot.pe_ttm is None:
        return None
    return clamp(decimal_to_float(snapshot.pe_ttm / Decimal("80")) or 0, 0, 1)


def long_short_crowding(value: Decimal | None) -> float | None:
    """多空比拥挤度。"""

    if value is None:
        return None
    return clamp(decimal_to_float(abs(value - Decimal("1"))) or 0, 0, 1)


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
