"""验证金融 Agent 模型路由策略。"""

from __future__ import annotations

from finance_agent.agents.runtime import ModelRoutingPolicy


def main() -> None:
    """执行模型路由策略冒烟。"""

    policy = ModelRoutingPolicy()
    primary = policy.route_primary(
        workflow_type="recommendation_decision",
        task="roundtable_discussion",
        asset_id="asset:smoke:model_router",
    )
    if primary.model_key != "deepseek-v4-pro":
        raise AssertionError("常规分析模型必须默认为 DeepSeek V4 Pro。")

    review_item = {
        "asset_id": "asset:smoke:model_router",
        "decision_type": "recommendation_swap",
        "trade_action": "swap",
        "requires_review": True,
        "reason": {
            "severity": "high",
            "data_quality_status": "available",
            "has_conflicting_signal": False,
        },
    }
    review = policy.build_review_result(
        workflow_type="recommendation_decision",
        review_item=review_item,
        decision_summary="换股动作属于高风险决策。",
    )
    if review["review_model"] != "gpt-5.5-pro":
        raise AssertionError("高风险复核必须路由到 GPT-5.5 Pro。")
    if review["review_status"] != "requires_model_review":
        raise AssertionError("高风险复核状态必须可审计。")
    if review["review_input"]["trade_action"] != "swap":
        raise AssertionError("复核输入必须保留交易动作。")

    print(
        {
            "primary_model": primary.model_key,
            "review_model": review["review_model"],
            "review_status": review["review_status"],
        }
    )


if __name__ == "__main__":
    main()
