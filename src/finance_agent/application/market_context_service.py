"""选股用大盘环境和买入阈值调节服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MarketRegime = Literal["trend_up", "range", "trend_down", "risk_off"]
RegimeStrength = Literal["low", "medium", "high"]
LEGACY_REGIME_MAP = {"bull": "trend_up", "bear": "trend_down", "range": "range"}
NEW_TO_LEGACY_REGIME = {
    "trend_up": "bull",
    "range": "range",
    "trend_down": "bear",
    "risk_off": "bear",
}


@dataclass(frozen=True)
class MarketRegimeInput:
    """大盘环境规则输入，全部来自已入库的指数、情绪和资金流事实。"""

    index_trend_20d: float
    index_trend_60d: float
    volatility_20d: float
    advance_decline_ratio: float
    limit_up_down_ratio: float
    northbound_flow_score: float = 0.0
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketRiskBudget:
    """市场状态对应的组合风险预算。"""

    total_exposure: float
    per_position_risk: float
    allow_new_buys: bool
    allow_sector_override: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "total_exposure": self.total_exposure,
            "per_position_risk": self.per_position_risk,
            "allow_new_buys": self.allow_new_buys,
            "allow_sector_override": self.allow_sector_override,
        }


@dataclass(frozen=True)
class MarketRegimeResult:
    """大盘环境判断结果。"""

    regime: MarketRegime
    strength: RegimeStrength
    risk_multiplier: float
    risk_budget: MarketRiskBudget
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()

    @property
    def legacy_regime(self) -> str:
        """返回旧版 bull/range/bear 名称供过渡期消费者使用。"""

        return NEW_TO_LEGACY_REGIME[self.regime]

    def to_dict(self) -> dict[str, object]:
        """转换为推荐 payload 可保存的结构。"""

        return {
            "regime": self.regime,
            "legacy_regime": self.legacy_regime,
            "strength": self.strength,
            "risk_multiplier": self.risk_multiplier,
            "risk_budget": self.risk_budget.to_dict(),
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
        }


class MarketRegimeService:
    """用可审计规则评估趋势、震荡和极端风险状态。"""

    def evaluate(self, data: MarketRegimeInput) -> MarketRegimeResult:
        """评估当前大盘环境。"""

        bear_score = 0
        bull_score = 0
        reasons = [
            f"20日趋势为 {format_percent(data.index_trend_20d)}。",
            f"60日趋势为 {format_percent(data.index_trend_60d)}。",
            f"20日波动率为 {format_percent(data.volatility_20d)}。",
            f"涨跌家数比为 {data.advance_decline_ratio:.2f}。",
            f"涨停/跌停比为 {data.limit_up_down_ratio:.2f}。",
            f"北向/资金流评分为 {data.northbound_flow_score:.2f}。",
        ]
        if data.index_trend_20d <= -0.05:
            bear_score += 1
        if data.index_trend_60d <= -0.08:
            bear_score += 1
        if data.volatility_20d >= 0.30:
            bear_score += 1
        if data.advance_decline_ratio < 0.70:
            bear_score += 1
        if data.limit_up_down_ratio < 0.50:
            bear_score += 1
        if data.northbound_flow_score <= -0.5:
            bear_score += 1

        if data.index_trend_20d >= 0.05:
            bull_score += 1
        if data.index_trend_60d >= 0.08:
            bull_score += 1
        if data.volatility_20d <= 0.20:
            bull_score += 1
        if data.advance_decline_ratio > 1.20:
            bull_score += 1
        if data.limit_up_down_ratio > 1.50:
            bull_score += 1
        if data.northbound_flow_score >= 0.5:
            bull_score += 1

        if (
            data.index_trend_20d <= -0.08
            and data.index_trend_60d <= -0.12
            and data.volatility_20d >= 0.35
            and data.advance_decline_ratio <= 0.35
            and data.limit_up_down_ratio <= 0.25
        ):
            return MarketRegimeResult(
                regime="risk_off",
                strength="high",
                risk_multiplier=1.5,
                risk_budget=MarketRiskBudget(0.0, 0.0, False, False),
                reasons=tuple(reasons),
                evidence_ids=data.evidence_ids,
            )
        if bear_score >= 4 and bear_score > bull_score:
            return MarketRegimeResult(
                regime="trend_down",
                strength=score_strength(bear_score),
                risk_multiplier=1.35 if bear_score >= 5 else 1.2,
                risk_budget=MarketRiskBudget(0.35, 0.005, True, True),
                reasons=tuple(reasons),
                evidence_ids=data.evidence_ids,
            )
        if bull_score >= 4 and bull_score > bear_score:
            return MarketRegimeResult(
                regime="trend_up",
                strength=score_strength(bull_score),
                risk_multiplier=0.85 if bull_score >= 5 else 0.95,
                risk_budget=MarketRiskBudget(1.0, 0.01, True, True),
                reasons=tuple(reasons),
                evidence_ids=data.evidence_ids,
            )
        return MarketRegimeResult(
            regime="range",
            strength="medium",
            risk_multiplier=1.0,
            risk_budget=MarketRiskBudget(0.7, 0.008, True, True),
            reasons=tuple(reasons),
            evidence_ids=data.evidence_ids,
        )


def score_strength(score: int) -> RegimeStrength:
    """把规则命中数量转换为强度。"""

    if score >= 5:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def adjust_buy_percentile_threshold(
    *,
    base_threshold: float,
    regime: str,
    timing_posture: str | None = None,
) -> float:
    """按大盘环境和用户择时姿态调节买入分位阈值。"""

    posture = (timing_posture or "balanced").lower()
    multiplier = 1.0
    normalized_regime = LEGACY_REGIME_MAP.get(regime, regime)
    if normalized_regime in {"trend_down", "risk_off"}:
        multiplier = 0.4 if posture == "defensive" else 0.7 if posture == "opportunistic" else 0.55
    elif normalized_regime == "trend_up":
        multiplier = 1.2 if posture == "opportunistic" else 1.0 if posture == "defensive" else 1.1
    return round(max(min(base_threshold * multiplier, 1.0), 0.01), 6)


def format_percent(value: float) -> str:
    """格式化比例。"""

    return f"{value * 100:.2f}%"
