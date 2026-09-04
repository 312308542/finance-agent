"""多日热门板块生命周期与弱市覆盖资格。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SectorRegime = Literal["ignition", "diffusion", "acceleration", "divergence", "cooling"]


@dataclass(frozen=True)
class SectorOpportunityHistory:
    """单个板块在同一决策时点可用的多日事实。"""

    sector_id: str
    excess_returns: dict[int, float]
    breadth: float
    ma20_ratio: float
    flow_streak: int
    leader_asset_ids: tuple[str, ...]
    challenger_asset_ids: tuple[str, ...]
    breadth_change: float = 0.0
    valid_cross_sections: int = 1
    previous_regime: SectorRegime | None = None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SectorOpportunity:
    """板块阶段、强度和市场弱势覆盖结果。"""

    sector_id: str
    regime: SectorRegime
    strength_score: float
    excess_returns: dict[int, float]
    breadth: float
    ma20_ratio: float
    flow_streak: int
    leader_asset_ids: tuple[str, ...]
    challenger_asset_ids: tuple[str, ...]
    override_eligible: bool
    chase_risk: bool
    maximum_sector_positions: int
    exposure_multiplier: float
    evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


class SectorOpportunityService:
    """使用进入/保留双阈值判断板块生命周期。"""

    def evaluate(
        self,
        history: SectorOpportunityHistory,
        *,
        market_regime: str,
    ) -> SectorOpportunity:
        """返回板块阶段和弱市覆盖资格，不产生订单。"""

        returns = {int(key): float(value) for key, value in history.excess_returns.items()}
        excess_1d = returns.get(1, 0.0)
        excess_3d = returns.get(3, 0.0)
        excess_5d = returns.get(5, 0.0)
        has_roles = bool(history.leader_asset_ids and history.challenger_asset_ids)
        entry_diffusion = (
            excess_3d >= 0.02
            and excess_5d >= 0.03
            and history.breadth >= 0.60
            and history.ma20_ratio >= 0.60
            and history.flow_streak >= 3
            and has_roles
        )
        retained_diffusion = (
            history.previous_regime in {"diffusion", "acceleration"}
            and excess_3d >= 0.01
            and excess_5d >= 0.02
            and history.breadth >= 0.50
            and history.ma20_ratio >= 0.50
            and history.flow_streak >= 2
            and has_roles
        )
        accelerating = entry_diffusion and (
            excess_3d >= 0.08 or (excess_1d >= 0.03 and excess_3d >= 0.06)
        )
        reasons: list[str] = []
        if accelerating:
            regime: SectorRegime = "acceleration"
        elif entry_diffusion and history.valid_cross_sections >= 2:
            regime = "diffusion"
        elif entry_diffusion:
            regime = "ignition"
            reasons.append("entry_confirmation_missing")
        elif retained_diffusion:
            regime = "diffusion"
            reasons.append("retention_threshold_met")
        elif excess_3d > 0 and history.breadth_change <= -0.10:
            regime = "divergence"
        elif excess_1d > 0 and history.flow_streak >= 1:
            regime = "ignition"
        else:
            regime = "cooling"

        chase_risk = regime == "acceleration" and history.breadth_change < 0
        healthy_diffusion = (
            regime == "diffusion"
            and history.valid_cross_sections >= 2
            and history.breadth >= 0.60
            and history.ma20_ratio >= 0.60
            and history.flow_streak >= 3
            and has_roles
            and not chase_risk
        )
        override_eligible = market_regime == "trend_down" and healthy_diffusion
        if market_regime == "risk_off":
            reasons.append("market_risk_off")
            override_eligible = False
        elif market_regime == "trend_down" and not healthy_diffusion:
            reasons.append("healthy_diffusion_missing")
        strength_score = _strength_score(history)
        return SectorOpportunity(
            sector_id=history.sector_id,
            regime=regime,
            strength_score=strength_score,
            excess_returns=returns,
            breadth=round(float(history.breadth), 6),
            ma20_ratio=round(float(history.ma20_ratio), 6),
            flow_streak=max(0, int(history.flow_streak)),
            leader_asset_ids=tuple(dict.fromkeys(history.leader_asset_ids)),
            challenger_asset_ids=tuple(dict.fromkeys(history.challenger_asset_ids)),
            override_eligible=override_eligible,
            chase_risk=chase_risk,
            maximum_sector_positions=3 if override_eligible else 0,
            exposure_multiplier=0.5 if override_eligible else 0.0,
            evidence_ids=tuple(dict.fromkeys(history.evidence_ids)),
            reason_codes=tuple(dict.fromkeys(reasons)),
        )


def _strength_score(history: SectorOpportunityHistory) -> float:
    returns = history.excess_returns
    return_score = min(
        max(float(returns.get(3, 0.0)), 0.0) * 300
        + max(float(returns.get(5, 0.0)), 0.0) * 200,
        40.0,
    )
    breadth_score = min(max(float(history.breadth), 0.0), 1.0) * 20
    ma_score = min(max(float(history.ma20_ratio), 0.0), 1.0) * 15
    flow_score = min(max(int(history.flow_streak), 0), 5) * 3
    role_score = min(
        len(history.leader_asset_ids) * 5 + len(history.challenger_asset_ids) * 2.5,
        10,
    )
    return round(min(return_score + breadth_score + ma_score + flow_score + role_score, 100), 6)
