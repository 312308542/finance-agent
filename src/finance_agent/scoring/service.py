"""透明评分服务。

ScoringService 消费 `screening_result_items` 和 `factor_frames`，按固定权重生成
`asset_scores`。第一版不调用 LLM，也不接入机器学习模型。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import FactorFrameORM
from finance_agent.storage.repositories import (
    AssetScoreRepository,
    FactorFrameRepository,
    ScreeningRepository,
)

JsonDict = dict[str, Any]

RULE_VERSION = "asset_score_v1.0.0"


@dataclass(frozen=True)
class ScoringRunResult:
    """一次评分摘要。"""

    status: str
    screening_id: str
    scored_count: int
    top_score_id: str | None


class ScoringService:
    """对通过初筛的标的生成透明评分。"""

    def __init__(self, session: Session) -> None:
        self.screenings = ScreeningRepository(session)
        self.factors = FactorFrameRepository(session)
        self.scores = AssetScoreRepository(session)

    def score_screening(
        self,
        *,
        screening_id: str,
        horizon: str = "swing",
        rule_version: str = RULE_VERSION,
    ) -> ScoringRunResult:
        """对一次初筛结果生成评分。"""

        items = self.screenings.list_items(screening_id=screening_id, passed_only=True)
        ranked_inputs: list[tuple[JsonDict, FactorFrameORM, Any]] = []
        for item in items:
            factor = self.factors.get_latest_factor_frame(asset_id=item.asset_id, horizon=horizon)
            if factor is None:
                continue
            score_payload = compute_asset_score(factor)
            ranked_inputs.append((score_payload, factor, item))

        ranked_inputs.sort(key=lambda item: item[0]["total_score"], reverse=True)
        saved_ids: list[str] = []
        for rank, (score_payload, factor, item) in enumerate(ranked_inputs, start=1):
            score_id = build_score_id(
                universe_id=item.universe_id,
                asset_id=item.asset_id,
                horizon=horizon,
                factor_frame_id=factor.factor_frame_id,
            )
            saved = self.scores.upsert_asset_score(
                score_id=score_id,
                asset_id=item.asset_id,
                symbol=item.symbol,
                market=item.market,
                universe_id=item.universe_id,
                screening_id=screening_id,
                factor_frame_id=factor.factor_frame_id,
                horizon=horizon,
                total_score=to_decimal(score_payload["total_score"]),
                technical_score=optional_decimal(score_payload["group_scores"].get("technical")),
                fundamental_score=optional_decimal(
                    score_payload["group_scores"].get("fundamental")
                ),
                valuation_score=optional_decimal(score_payload["group_scores"].get("valuation")),
                flow_score=optional_decimal(score_payload["group_scores"].get("capital_flow")),
                derivatives_score=optional_decimal(
                    score_payload["group_scores"].get("derivatives")
                ),
                event_score=optional_decimal(score_payload["group_scores"].get("event")),
                risk_penalty=to_decimal(score_payload["risk_penalty"]),
                rank=rank,
                rank_in_universe=rank,
                confidence=to_decimal(score_payload["confidence"]),
                missing_penalty=to_decimal(score_payload["missing_penalty"]),
                rule_version=rule_version,
                status=score_payload["status"],
                as_of=factor.as_of,
                payload={
                    "schema_version": "1.0",
                    "score_groups": score_payload["score_groups"],
                    "missing_groups": factor.missing_groups,
                    "partial_groups": factor.payload.get("partial_groups", []),
                    "source_ids": factor.source_ids,
                    "policy": {
                        "weighting": "renormalize_available_groups",
                        "missing_penalty_per_group": 4,
                        "llm_role": "explanation_only",
                    },
                },
            )
            saved_ids.append(saved.score_id)

        return ScoringRunResult(
            status="available" if saved_ids else "unavailable",
            screening_id=screening_id,
            scored_count=len(saved_ids),
            top_score_id=saved_ids[0] if saved_ids else None,
        )


def compute_asset_score(factor: FactorFrameORM) -> JsonDict:
    """计算单标的透明评分。"""

    groups = {group.get("group"): group for group in factor.payload.get("factor_groups") or []}
    weights = score_weights(factor.market)
    score_groups: list[JsonDict] = []
    group_scores: dict[str, float | None] = {}
    weighted_sum = 0.0
    used_weight = 0.0

    for group_name, weight in weights.items():
        group = groups.get(group_name)
        score = as_float(group.get("score")) if group else None
        group_scores[group_name] = score
        if score is None:
            continue
        contribution = score * weight
        weighted_sum += contribution
        used_weight += weight
        score_groups.append(
            {
                "group": group_name,
                "score": score,
                "weight": weight,
                "contribution": contribution,
                "status": group.get("status") if group else "unavailable",
            }
        )

    base_score = weighted_sum / used_weight if used_weight > 0 else 0.0
    risk_penalty = compute_risk_penalty(groups.get("risk"))
    missing_penalty = len(factor.missing_groups) * 4 + len(
        factor.payload.get("partial_groups") or []
    ) * 1.5
    total_score = clamp(base_score - missing_penalty - risk_penalty, 0, 100)
    confidence = clamp(used_weight / sum(weights.values()) - missing_penalty / 100 * 0.4, 0.05, 1)
    status = "available" if factor.status == "available" and confidence >= 0.6 else "partial"
    if used_weight == 0:
        status = "unavailable"

    return {
        "status": status,
        "total_score": total_score,
        "confidence": confidence,
        "risk_penalty": risk_penalty,
        "missing_penalty": missing_penalty,
        "group_scores": group_scores,
        "score_groups": score_groups,
    }


def score_weights(market: str) -> dict[str, float]:
    """返回市场专用的第一版评分权重。"""

    if market.startswith("crypto"):
        return {
            "technical": 0.38,
            "derivatives": 0.25,
            "liquidity": 0.12,
            "event": 0.10,
            "event_decay": 0.05,
            "risk": 0.10,
        }
    return {
        "technical": 0.28,
        "fundamental": 0.22,
        "valuation": 0.15,
        "capital_flow": 0.15,
        "liquidity": 0.08,
        "event": 0.07,
        "event_decay": 0.02,
        "risk": 0.03,
    }


def compute_risk_penalty(group: JsonDict | None) -> float:
    """把风险组转换成扣分。"""

    if group is None:
        return 0.0
    score = as_float(group.get("score"))
    if score is None:
        return 0.0
    return clamp((60 - score) * 0.3, 0, 25)


def build_score_id(
    *,
    universe_id: str,
    asset_id: str,
    horizon: str,
    factor_frame_id: str,
) -> str:
    """生成稳定评分 ID。"""

    return f"score:{universe_id}:{asset_id}:{horizon}:{factor_frame_id}"


def as_float(value: Any) -> float | None:
    """安全转浮点。"""

    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def to_decimal(value: float) -> Decimal:
    """浮点转 Decimal。"""

    return Decimal(str(round(value, 6)))


def optional_decimal(value: float | None) -> Decimal | None:
    """可空浮点转 Decimal。"""

    return to_decimal(value) if value is not None else None


def clamp(value: float, low: float, high: float) -> float:
    """裁剪数值区间。"""

    return max(low, min(high, value))
