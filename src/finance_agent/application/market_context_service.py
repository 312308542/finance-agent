"""选股用大盘环境和买入阈值调节服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MarketRegime = Literal["bull", "bear", "range"]
RegimeStrength = Literal["low", "medium", "high"]


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
class MarketRegimeResult:
    """大盘环境判断结果。"""

    regime: MarketRegime
    strength: RegimeStrength
    risk_multiplier: float
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """转换为推荐 payload 可保存的结构。"""

        return {
            "regime": self.regime,
            "strength": self.strength,
            "risk_multiplier": self.risk_multiplier,
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
        }


class MarketRegimeService:
    """用可审计规则把大盘状态粗分为牛市、弱市或震荡。"""

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

        if bear_score >= 4 and bear_score > bull_score:
            return MarketRegimeResult(
                regime="bear",
                strength=score_strength(bear_score),
                risk_multiplier=1.35 if bear_score >= 5 else 1.2,
                reasons=tuple(reasons),
                evidence_ids=data.evidence_ids,
            )
        if bull_score >= 4 and bull_score > bear_score:
            return MarketRegimeResult(
                regime="bull",
                strength=score_strength(bull_score),
                risk_multiplier=0.85 if bull_score >= 5 else 0.95,
                reasons=tuple(reasons),
                evidence_ids=data.evidence_ids,
            )
        return MarketRegimeResult(
            regime="range",
            strength="medium",
            risk_multiplier=1.0,
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
    if regime == "bear":
        multiplier = 0.4 if posture == "defensive" else 0.7 if posture == "opportunistic" else 0.55
    elif regime == "bull":
        multiplier = 1.2 if posture == "opportunistic" else 1.0 if posture == "defensive" else 1.1
    return round(max(min(base_threshold * multiplier, 1.0), 0.01), 6)


def format_percent(value: float) -> str:
    """格式化比例。"""

    return f"{value * 100:.2f}%"
