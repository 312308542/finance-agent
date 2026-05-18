"""内部金融 Agent Loop 的确定性规划器。"""

from __future__ import annotations

from hashlib import sha1

from finance_agent.agents.loop.state import AgentLoopContext, AgentLoopPlan

SUPPORTED_WORKFLOW_TYPES = {
    "portfolio_monitoring",
    "watchlist_management",
    "recommendation_decision",
    "asset_deep_analysis",
    "swap_decision",
    "daily_review",
}


class InternalFinanceAgentPlanner:
    """把触发事件转换为受控的工具和 Workflow 调用计划。"""

    def build_plan(self, context: AgentLoopContext) -> AgentLoopPlan:
        """基于已入库触发事实生成执行计划。"""

        event = context.event
        payload = event.payload or {}
        completed_status = payload.get("agent_loop_status")
        if completed_status in {"workflow_completed", "skipped", "failed"}:
            return AgentLoopPlan(
                action="skip",
                reason=f"Agent 任务已经处理过：{completed_status}。",
            )

        workflow_type = event.requested_workflow_type
        if workflow_type not in SUPPORTED_WORKFLOW_TYPES:
            return AgentLoopPlan(
                action="skip",
                reason=f"不支持的 Workflow 类型：{workflow_type}。",
            )

        missing_reason = validate_workflow_context(event)
        if missing_reason:
            return AgentLoopPlan(action="skip", reason=missing_reason)

        workflow_run_id = build_internal_agent_workflow_run_id(
            owner_id=event.owner_id,
            workflow_type=workflow_type,
            agent_task_id=event.agent_task_id or event.trigger_event_id,
        )
        initial_state = {
            "owner_id": event.owner_id,
            "portfolio_id": event.portfolio_id,
            "watchlist_id": event.watchlist_id,
            "recommendation_run_id": event.recommendation_run_id,
            "asset_id": event.asset_id,
            "asset_ids": [event.asset_id] if event.asset_id else [],
            "horizon": payload.get("horizon") or "swing",
            "timeframe": payload.get("timeframe") or "1d",
            "recommendation_limit": int(payload.get("recommendation_limit") or 20),
            "trigger_event": {
                "trigger_event_id": event.trigger_event_id,
                "trigger_type": event.trigger_type,
                "trigger_ref": event.trigger_ref,
                "severity": event.severity,
                "reason": payload.get("reason"),
            },
        }
        return AgentLoopPlan(
            action="run_workflow",
            reason=f"触发事件建议执行 {workflow_type}，内部 Agent 选择调用底层金融团队 Workflow。",
            workflow_type=workflow_type,
            workflow_run_id=workflow_run_id,
            initial_state=initial_state,
            tool_calls=build_planned_tool_calls(event),
        )


def validate_workflow_context(event: object) -> str | None:
    """校验 Workflow 调用所需上下文。"""

    workflow_type = event.requested_workflow_type
    if workflow_type in {"portfolio_monitoring", "recommendation_decision"}:
        if not event.portfolio_id:
            return f"{workflow_type} 缺少 portfolio_id，内部 Agent 跳过。"
    if workflow_type in {"watchlist_management", "recommendation_decision"}:
        if not event.watchlist_id:
            return f"{workflow_type} 缺少 watchlist_id，内部 Agent 跳过。"
    if workflow_type == "recommendation_decision" and not event.recommendation_run_id:
        return "recommendation_decision 缺少 recommendation_run_id，内部 Agent 跳过。"
    if workflow_type in {"asset_deep_analysis", "swap_decision"} and not event.asset_id:
        return f"{workflow_type} 缺少 asset_id，内部 Agent 跳过。"
    return None


def build_planned_tool_calls(event: object) -> tuple[dict[str, object], ...]:
    """记录内部 Agent 决策前预计会读取的事实工具。"""

    calls: list[dict[str, object]] = []
    if event.portfolio_id:
        calls.append({"tool": "portfolio.get_snapshot", "portfolio_id": event.portfolio_id})
    if event.watchlist_id:
        calls.append(
            {
                "tool": "watchlist.get_active_items",
                "owner_id": event.owner_id,
                "watchlist_id": event.watchlist_id,
            }
        )
    if event.recommendation_run_id:
        calls.append(
            {"tool": "recommendation.get_run", "run_id": event.recommendation_run_id}
        )
    if event.asset_id:
        calls.extend(
            (
                {
                    "tool": "factor.get_asset_factor_context",
                    "asset_id": event.asset_id,
                },
                {
                    "tool": "signal_risk.get_asset_context",
                    "asset_id": event.asset_id,
                },
                {
                    "tool": "memory.recall_asset_memories",
                    "owner_id": event.owner_id,
                    "asset_id": event.asset_id,
                },
            )
        )
    return tuple(calls)


def build_internal_agent_workflow_run_id(
    *,
    owner_id: str,
    workflow_type: str,
    agent_task_id: str,
) -> str:
    """生成内部 Agent Loop 调用 Workflow 的幂等运行 ID。"""

    clean_owner = owner_id.replace(":", "_")
    digest = sha1(agent_task_id.encode()).hexdigest()[:16]
    return f"workflow:{clean_owner}:internal_loop:{workflow_type}:{digest}"
