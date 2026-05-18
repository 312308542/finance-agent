"""Workflow 复核策略。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewDecisionContext:
    """高风险复核判断上下文。"""

    decision_type: str
    suggested_action: str
    severity: str
    confidence: float
    data_quality_status: str
    risk_severities: tuple[str, ...] = ()
    has_conflicting_signal: bool = False


class HighRiskReviewPolicy:
    """判断某个决策是否必须升级到 GPT-5.5 Pro 复核。"""

    review_actions = {"sell", "swap", "reduce", "add_to_watchlist"}
    review_decision_types = {
        "recommendation_sell",
        "recommendation_swap",
        "recommendation_reject",
    }

    def requires_review(self, context: ReviewDecisionContext) -> bool:
        """判断是否需要高风险复核。"""

        if context.data_quality_status in {"missing", "stale", "partial"}:
            return True
        if context.has_conflicting_signal:
            return True
        if context.severity in {"high", "critical"}:
            return True
        if context.suggested_action in self.review_actions:
            return True
        if context.decision_type in self.review_decision_types:
            return True
        if any(severity in {"high", "critical"} for severity in context.risk_severities):
            return True
        return context.confidence < 0.75
