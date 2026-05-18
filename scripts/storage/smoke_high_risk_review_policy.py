"""验证高风险复核策略。"""

from __future__ import annotations

from finance_agent.agents.runtime import HighRiskReviewPolicy, ReviewDecisionContext


def main() -> None:
    """执行高风险复核策略冒烟。"""

    policy = HighRiskReviewPolicy()
    swap_context = ReviewDecisionContext(
        decision_type="recommendation_swap",
        suggested_action="swap",
        severity="high",
        confidence=0.72,
        data_quality_status="available",
        risk_severities=("medium",),
        has_conflicting_signal=False,
    )
    if not policy.requires_review(swap_context):
        raise AssertionError("换股动作必须触发高风险复核")

    watch_context = ReviewDecisionContext(
        decision_type="recommendation_watch",
        suggested_action="watch",
        severity="low",
        confidence=0.86,
        data_quality_status="available",
        risk_severities=(),
        has_conflicting_signal=False,
    )
    if policy.requires_review(watch_context):
        raise AssertionError("普通观察动作不应触发高风险复核")

    missing_data_context = ReviewDecisionContext(
        decision_type="recommendation_buy",
        suggested_action="buy",
        severity="medium",
        confidence=0.81,
        data_quality_status="missing",
        risk_severities=(),
        has_conflicting_signal=False,
    )
    if not policy.requires_review(missing_data_context):
        raise AssertionError("数据缺口下的买入动作必须触发复核")

    print(
        {
            "swap_review": policy.requires_review(swap_context),
            "watch_review": policy.requires_review(watch_context),
            "missing_data_review": policy.requires_review(missing_data_context),
        }
    )


if __name__ == "__main__":
    main()
