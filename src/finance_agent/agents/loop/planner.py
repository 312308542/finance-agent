"""内部金融 Agent Loop 的确定性规划器。"""

from __future__ import annotations

from hashlib import sha1
from typing import Any

from finance_agent.agents.runtime.model_config import ModelRegistry, load_model_registry
from finance_agent.agents.runtime.model_router import ModelRoutingPolicy
from finance_agent.agents.tools.runtime import FinanceToolRuntime
from finance_agent.agents.loop.state import AgentLoopContext, AgentLoopPlan
from finance_agent.graph.stores import DryRunGraphStore

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


class ModelFinanceAgentPlanner:
    """模型增强版内部 Agent Planner。

    当前阶段先完成“模型可接入”的规划层闭环：用确定性 planner 保证执行边界，
    再把模型路由、可调用工具、图谱记忆召回和 dry-run 状态写入审计上下文。
    当真实模型端点配置完成后，上层模型客户端可以替换 `_build_model_decision`。
    """

    def __init__(
        self,
        *,
        model_registry: ModelRegistry | None = None,
        tool_runtime: FinanceToolRuntime | None = None,
        fallback_planner: InternalFinanceAgentPlanner | None = None,
        execute_fact_tools: bool = True,
    ) -> None:
        self.model_registry = model_registry
        self.tool_runtime = tool_runtime
        self.fallback_planner = fallback_planner or InternalFinanceAgentPlanner()
        self.execute_fact_tools = execute_fact_tools
        self.routing_policy = ModelRoutingPolicy()

    def build_plan(self, context: AgentLoopContext) -> AgentLoopPlan:
        """生成模型增强计划，并保留确定性 fallback 的安全边界。"""

        fallback_plan = self.fallback_planner.build_plan(context)
        if fallback_plan.action != "run_workflow":
            return fallback_plan

        event = context.event
        workflow_type = fallback_plan.workflow_type or event.requested_workflow_type
        route = self.routing_policy.route_primary(
            workflow_type=workflow_type,
            task="internal_agent_planning",
            asset_id=event.asset_id,
            decision_type=event.trigger_type,
            reason="内部 Agent Loop 规划优先使用 DeepSeek V4 Pro；未配置端点时走 dry-run fallback。",
        )
        registry = self._load_registry()
        config = registry.get(route.model_key)
        model_ready = bool(config and config.ready)
        extended_tool_calls = build_model_planned_tool_calls(event)
        tool_results_summary = self._collect_tool_results(
            context=context,
            tool_calls=extended_tool_calls,
        )
        model_decision = self._build_model_decision(
            context=context,
            route=route.to_dict(),
            model_ready=model_ready,
            registry_source=registry.source,
            tool_results_summary=tool_results_summary,
            fallback_plan=fallback_plan,
        )
        initial_state = dict(fallback_plan.initial_state)
        initial_state["model_planner"] = model_decision
        initial_state["model_routes"] = [route.to_dict()]
        initial_state["planned_tool_calls"] = list(extended_tool_calls)
        initial_state["tool_results_summary"] = tool_results_summary
        return AgentLoopPlan(
            action=fallback_plan.action,
            reason=model_decision["reason"],
            workflow_type=fallback_plan.workflow_type,
            workflow_run_id=fallback_plan.workflow_run_id,
            initial_state=initial_state,
            tool_calls=extended_tool_calls,
        )

    def _load_registry(self) -> ModelRegistry:
        if self.model_registry is not None:
            return self.model_registry
        return load_model_registry()

    def _resolve_tool_runtime(self, context: AgentLoopContext) -> FinanceToolRuntime | None:
        if self.tool_runtime is not None:
            return self.tool_runtime
        if context.session is None:
            return None
        return FinanceToolRuntime(context.session, graph_store=DryRunGraphStore())

    def _collect_tool_results(
        self,
        *,
        context: AgentLoopContext,
        tool_calls: tuple[dict[str, object], ...],
    ) -> list[dict[str, Any]]:
        """执行只读事实工具并压缩成模型 Planner 可审计摘要。"""

        if not self.execute_fact_tools:
            return []
        runtime = self._resolve_tool_runtime(context)
        if runtime is None:
            return [
                {
                    "tool": call.get("tool"),
                    "status": "skipped",
                    "reason": "缺少数据库会话，模型 Planner 未执行事实工具。",
                }
                for call in tool_calls
            ]

        summaries: list[dict[str, Any]] = []
        for call in tool_calls[: context.limits.max_tool_calls]:
            tool_name = str(call.get("tool") or "")
            kwargs = {key: value for key, value in call.items() if key != "tool"}
            try:
                result = runtime.call(tool_name, **kwargs)
                summaries.append(
                    {
                        "tool": tool_name,
                        "status": "ok",
                        "summary": summarize_tool_result(result),
                    }
                )
            except Exception as exc:
                summaries.append(
                    {
                        "tool": tool_name,
                        "status": "failed",
                        "error": str(exc),
                    }
                )
        return summaries

    def _build_model_decision(
        self,
        *,
        context: AgentLoopContext,
        route: dict[str, Any],
        model_ready: bool,
        registry_source: str,
        tool_results_summary: list[dict[str, Any]],
        fallback_plan: AgentLoopPlan,
    ) -> dict[str, Any]:
        """构建模型 Planner 审计输出。"""

        event = context.event
        status = "ready_for_model_call" if model_ready else "dry_run_fallback"
        reason = (
            f"模型 Planner 已为 {fallback_plan.workflow_type} 生成工具和 Workflow 计划；"
            "当前模型端点已具备真实调用配置。"
            if model_ready
            else (
                f"模型 Planner 未检测到真实模型端点配置，按已入库触发事件、"
                f"事实工具和确定性 fallback 执行 {fallback_plan.workflow_type}。"
            )
        )
        return {
            "planner": "ModelFinanceAgentPlanner",
            "status": status,
            "reason": reason,
            "model_call_executed": False,
            "registry_source": registry_source,
            "route": route,
            "trigger_event_id": event.trigger_event_id,
            "requested_workflow_type": event.requested_workflow_type,
            "tool_result_count": len(tool_results_summary),
            "fallback_action": fallback_plan.action,
        }


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


def build_model_planned_tool_calls(event: object) -> tuple[dict[str, object], ...]:
    """构建模型 Planner 可按需调用的工具计划。"""

    calls = list(build_planned_tool_calls(event))
    if event.asset_id:
        calls.extend(
            (
                {
                    "tool": "memory.trace_asset_graph",
                    "owner_id": event.owner_id,
                    "asset_id": event.asset_id,
                    "max_depth": 2,
                    "limit": 20,
                },
                {
                    "tool": "memory.explain_candidate_reason_chain",
                    "owner_id": event.owner_id,
                    "asset_id": event.asset_id,
                    "limit": 5,
                },
                {
                    "tool": "memory.find_memory_conflicts",
                    "owner_id": event.owner_id,
                    "asset_id": event.asset_id,
                    "limit": 10,
                },
                {
                    "tool": "memory.find_similar_decision_paths",
                    "owner_id": event.owner_id,
                    "asset_id": event.asset_id,
                    "limit": 10,
                },
            )
        )
    elif event.owner_id:
        calls.append(
            {
                "tool": "memory.find_memory_conflicts",
                "owner_id": event.owner_id,
                "limit": 10,
            }
        )
    return tuple(dedupe_tool_calls(calls))


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


def dedupe_tool_calls(calls: list[dict[str, object]]) -> list[dict[str, object]]:
    """按工具名和参数对工具计划去重。"""

    seen: set[tuple[tuple[str, str], ...]] = set()
    result: list[dict[str, object]] = []
    for call in calls:
        key = tuple(sorted((str(item_key), str(item_value)) for item_key, item_value in call.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(call)
    return result


def summarize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """压缩工具结果，避免把大段事实重复写入 Planner 审计。"""

    summary: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, list):
            summary[key] = {
                "count": len(value),
                "sample": value[:2],
            }
        elif isinstance(value, dict):
            summary[key] = {
                "keys": sorted(str(item_key) for item_key in value.keys())[:20],
            }
        else:
            summary[key] = value
    return summary


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
