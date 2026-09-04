"""同一决策快照内的截面标准化与自适应 Alpha。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

JsonDict = dict[str, Any]
ADAPTIVE_GROUPS = (
    "trend",
    "structure",
    "sector_leadership",
    "capital_flow",
    "fundamental_valuation",
    "tradability_return_risk",
)
BASELINE_ADAPTIVE_WEIGHTS = {
    "trend": 0.25,
    "structure": 0.20,
    "sector_leadership": 0.20,
    "capital_flow": 0.15,
    "fundamental_valuation": 0.10,
    "tradability_return_risk": 0.10,
}
REGIME_WEIGHTS = {
    "trend_up": BASELINE_ADAPTIVE_WEIGHTS,
    "range": {
        "trend": 0.20,
        "structure": 0.22,
        "sector_leadership": 0.18,
        "capital_flow": 0.14,
        "fundamental_valuation": 0.13,
        "tradability_return_risk": 0.13,
    },
    "trend_down": {
        "trend": 0.15,
        "structure": 0.25,
        "sector_leadership": 0.25,
        "capital_flow": 0.15,
        "fundamental_valuation": 0.08,
        "tradability_return_risk": 0.12,
    },
    "risk_off": {
        "trend": 0.10,
        "structure": 0.25,
        "sector_leadership": 0.15,
        "capital_flow": 0.10,
        "fundamental_valuation": 0.10,
        "tradability_return_risk": 0.30,
    },
}


@dataclass(frozen=True)
class AdaptiveAssetInput:
    """一个资产在统一决策时点的六组评分输入。"""

    asset_id: str
    as_of: datetime
    group_scores: Mapping[str, float | int | None]
    factor_as_of: Mapping[str, datetime]
    data_quality: str = "available"
    missing_groups: tuple[str, ...] = ()
    partial_groups: tuple[str, ...] = ()
    expected_return_hint: float | None = None
    downside_risk: float = 0.0


@dataclass(frozen=True)
class AlphaEstimate:
    """自适应 Alpha、收益风险和可买入资格。"""

    alpha_score: float
    expected_net_return: float
    downside_risk: float
    confidence: float
    eligible_for_buy: bool
    reason_codes: tuple[str, ...]
    contributions: tuple[JsonDict, ...]


class AdaptiveAlphaEngine:
    """在同一快照内标准化并计算自适应 Alpha。"""

    def score(
        self,
        assets: Sequence[AdaptiveAssetInput],
        *,
        market_regime: str,
    ) -> tuple[AlphaEstimate, ...]:
        weights = adaptive_group_weights(market_regime)
        normalized_by_group = {
            group: normalize_cross_section(
                {
                    asset.asset_id: float(value)
                    for asset in assets
                    if (value := asset.group_scores.get(group)) is not None
                    and math.isfinite(float(value))
                }
            )
            for group in ADAPTIVE_GROUPS
        }
        results: list[AlphaEstimate] = []
        for asset in assets:
            reasons: list[str] = []
            asset_as_of = _utc(asset.as_of)
            if any(_utc(value) > asset_as_of for value in asset.factor_as_of.values()):
                reasons.append("future_factor_data")
            if asset.data_quality != "available":
                reasons.append(f"data_quality_{asset.data_quality}")
            if "tradability_return_risk" in asset.missing_groups:
                reasons.append("risk_group_missing")
            contributions: list[JsonDict] = []
            alpha_score = 0.0
            available_weight = 0.0
            for group in ADAPTIVE_GROUPS:
                raw = asset.group_scores.get(group)
                normalized = normalized_by_group[group].get(asset.asset_id)
                weight = weights[group]
                status = (
                    "missing"
                    if group in asset.missing_groups or raw is None
                    else "partial"
                    if group in asset.partial_groups
                    else "available"
                )
                contribution = (normalized or 0.0) * weight
                alpha_score += contribution
                if status == "available":
                    available_weight += weight
                elif status == "partial":
                    available_weight += weight * 0.5
                contributions.append(
                    {
                        "group": group,
                        "raw_score": float(raw) if raw is not None else None,
                        "normalized_score": normalized,
                        "weight": weight,
                        "contribution": round(contribution, 6),
                        "status": status,
                    }
                )
            confidence = _clamp(
                available_weight
                - len(asset.missing_groups) * 0.08
                - len(asset.partial_groups) * 0.04,
                0.0,
                1.0,
            )
            expected_return = (
                float(asset.expected_return_hint)
                if asset.expected_return_hint is not None
                else (alpha_score - 50.0) / 100 * 0.12 - 0.003
            )
            downside_risk = max(0.0, float(asset.downside_risk))
            if confidence < 0.75:
                reasons.append("confidence_below_0_75")
            if expected_return <= 0.003 + downside_risk * 0.25:
                reasons.append("expected_return_below_risk_buffer")
            if market_regime == "risk_off":
                reasons.append("market_risk_off")
            results.append(
                AlphaEstimate(
                    alpha_score=round(_clamp(alpha_score, 0.0, 100.0), 6),
                    expected_net_return=round(expected_return, 6),
                    downside_risk=round(downside_risk, 6),
                    confidence=round(confidence, 6),
                    eligible_for_buy=not reasons,
                    reason_codes=tuple(dict.fromkeys(reasons)),
                    contributions=tuple(contributions),
                )
            )
        return tuple(results)


def normalize_cross_section(values: Mapping[str, float | int]) -> dict[str, float]:
    """按唯一值秩映射到 0~100；常数截面统一返回 50。"""

    cleaned = {
        str(key): float(value)
        for key, value in values.items()
        if math.isfinite(float(value))
    }
    unique = sorted(set(cleaned.values()))
    if not unique:
        return {}
    if len(unique) == 1:
        return {key: 50.0 for key in cleaned}
    rank = {value: index / (len(unique) - 1) * 100 for index, value in enumerate(unique)}
    return {key: round(rank[value], 6) for key, value in cleaned.items()}


def normalize_macd_signal(*, macd: float, atr: float | None, price: float) -> float:
    """优先按 ATR、缺失时按价格归一 MACD，消除绝对价格尺度。"""

    denominator = float(atr or 0)
    if denominator <= 0:
        denominator = abs(float(price))
    if denominator <= 0:
        return 0.0
    return _clamp(float(macd) / denominator, -5.0, 5.0)


def adaptive_group_weights(market_regime: str) -> dict[str, float]:
    """返回六组且权重和严格为 1 的市场状态权重。"""

    normalized = {"bull": "trend_up", "bear": "trend_down"}.get(
        market_regime,
        market_regime,
    )
    weights = dict(REGIME_WEIGHTS.get(normalized, REGIME_WEIGHTS["range"]))
    total = sum(weights.values())
    return {group: weight / total for group, weight in weights.items()}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
