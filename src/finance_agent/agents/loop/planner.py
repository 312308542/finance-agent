"""内部金融 Agent Loop 的规划器。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha1
from typing import Any

from finance_agent.agents.loop.state import AgentLoopContext, AgentLoopPlan
from finance_agent.agents.runtime.context_envelope import build_workflow_context_envelope
from finance_agent.agents.runtime.model_client import (
    ModelClient,
    ModelClientResponse,
    OpenAICompatibleModelClient,
)
from finance_agent.agents.runtime.model_config import ModelRegistry, load_model_registry
from finance_agent.agents.runtime.model_router import ModelRoutingPolicy
from finance_agent.agents.runtime.prompts import build_prompt_bundle
from finance_agent.agents.tools.runtime import FinanceToolRuntime
from finance_agent.graph.stores import DryRunGraphStore

SUPPORTED_WORKFLOW_TYPES = {
    "portfolio_monitoring",
    "watchlist_management",
    "recommendation_decision",
    "asset_deep_analysis",
    "swap_decision",
    "daily_review",
}


@dataclass(frozen=True)
class ModelPlannerLoopResult:
    """模型 Planner 循环的审计结果。"""

    status: str
    iterations: int
    final_decision: dict[str, Any] | None
    tool_observations: list[dict[str, Any]]
    tool_results_summary: list[dict[str, Any]]
    message_audit: list[dict[str, Any]]
    error_message: str | None = None


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

    有真实模型端点时，Planner 会进行受控的“模型 -> 工具 -> 观察 -> 模型”循环，
    让模型按需读取已入库事实、Workflow、记忆和图谱工具；没有模型或模型失败时，
    回到确定性 planner，保证触发事件仍能闭环。
    """

    def __init__(
        self,
        *,
        model_registry: ModelRegistry | None = None,
        tool_runtime: FinanceToolRuntime | None = None,
        model_client: ModelClient | None = None,
        fallback_planner: InternalFinanceAgentPlanner | None = None,
        execute_fact_tools: bool = True,
    ) -> None:
        self.model_registry = model_registry
        self.tool_runtime = tool_runtime
        self.model_client = model_client or OpenAICompatibleModelClient()
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
            reason=(
                "内部 Agent Loop 规划优先使用 DeepSeek V4 Pro；"
                "未配置端点时走 dry-run fallback。"
            ),
        )
        registry = self._load_registry()
        config = registry.get(route.model_key)
        model_ready = bool(config and config.ready)
        extended_tool_calls = build_model_planned_tool_calls(event)
        allowed_tool_calls = tuple(extended_tool_calls)
        tool_results_summary = []
        model_loop_result: ModelPlannerLoopResult | None = None
        prompt_envelope = build_planner_prompt_envelope(
            event=event,
            workflow_type=workflow_type,
            tool_calls=extended_tool_calls,
            tool_results_summary=tool_results_summary,
        )
        prompt_bundle = build_prompt_bundle(
            model_role="primary_financial_analyst",
            context_envelope=prompt_envelope,
        )
        if model_ready and config is not None:
            model_loop_result = self._run_model_loop(
                context=context,
                config=config,
                fallback_plan=fallback_plan,
                prompt_bundle=prompt_bundle,
                prompt_envelope=prompt_envelope,
                allowed_tool_calls=allowed_tool_calls,
            )
            if model_loop_result.tool_results_summary:
                tool_results_summary = model_loop_result.tool_results_summary
                prompt_envelope = build_planner_prompt_envelope(
                    event=event,
                    workflow_type=workflow_type,
                    tool_calls=allowed_tool_calls,
                    tool_results_summary=tool_results_summary,
                )
                prompt_bundle = build_prompt_bundle(
                    model_role="primary_financial_analyst",
                    context_envelope=prompt_envelope,
                )
        elif self.execute_fact_tools:
            tool_results_summary = self._collect_tool_results(
                context=context,
                tool_calls=extended_tool_calls,
            )
            prompt_envelope = build_planner_prompt_envelope(
                event=event,
                workflow_type=workflow_type,
                tool_calls=extended_tool_calls,
                tool_results_summary=tool_results_summary,
            )
            prompt_bundle = build_prompt_bundle(
                model_role="primary_financial_analyst",
                context_envelope=prompt_envelope,
            )
        model_decision = self._build_model_decision(
            context=context,
            route=route.to_dict(),
            model_ready=model_ready,
            registry_source=registry.source,
            tool_results_summary=tool_results_summary,
            fallback_plan=fallback_plan,
            prompt_bundle=prompt_bundle,
            model_loop_result=model_loop_result,
        )
        selected_plan = self._apply_model_loop_result(
            fallback_plan=fallback_plan,
            model_loop_result=model_loop_result,
            model_decision=model_decision,
        )
        initial_state = dict(fallback_plan.initial_state)
        initial_state["model_planner"] = model_decision
        initial_state["model_routes"] = [route.to_dict()]
        initial_state["planned_tool_calls"] = list(extended_tool_calls)
        initial_state["tool_results_summary"] = tool_results_summary
        initial_state["model_prompt_bundle"] = prompt_bundle
        initial_state["model_prompt_envelope"] = prompt_envelope
        if model_loop_result is not None:
            initial_state["model_loop_messages"] = model_loop_result.message_audit
            initial_state["model_tool_observations"] = model_loop_result.tool_observations
            initial_state["model_final_decision"] = model_loop_result.final_decision
        return AgentLoopPlan(
            action=selected_plan.action,
            reason=model_decision["reason"],
            workflow_type=selected_plan.workflow_type,
            workflow_run_id=selected_plan.workflow_run_id,
            initial_state=initial_state,
            tool_calls=tuple(
                selected_plan.tool_calls
                or tuple(
                    observation
                    for observation in (
                        model_loop_result.tool_observations if model_loop_result else ()
                    )
                )
                or extended_tool_calls
            ),
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

    def _run_model_loop(
        self,
        *,
        context: AgentLoopContext,
        config: Any,
        fallback_plan: AgentLoopPlan,
        prompt_bundle: dict[str, Any],
        prompt_envelope: dict[str, Any],
        allowed_tool_calls: tuple[dict[str, object], ...],
    ) -> ModelPlannerLoopResult:
        """运行受控模型规划循环。"""

        allowed_tools = build_allowed_tool_map(allowed_tool_calls)
        tool_observations: list[dict[str, Any]] = []
        tool_results_summary: list[dict[str, Any]] = []
        message_audit: list[dict[str, Any]] = []
        final_decision: dict[str, Any] | None = None
        error_message: str | None = None

        for iteration in range(1, max(context.limits.max_steps, 1) + 1):
            try:
                response = self.model_client.invoke_json(
                    config=config,
                    messages=build_model_planner_messages(
                        prompt_bundle=prompt_bundle,
                        prompt_envelope=prompt_envelope,
                        fallback_plan=fallback_plan,
                        allowed_tool_calls=allowed_tool_calls,
                        tool_observations=tool_observations,
                    ),
                    temperature=0.1,
                )
            except Exception as exc:
                error_message = str(exc)
                break

            message_audit.append(build_model_response_audit(response, iteration=iteration))
            decision = response.parsed_json or {}
            if not decision:
                error_message = "模型未返回可解析 JSON，已回退确定性计划。"
                break

            requested_tools = normalize_model_tool_requests(
                decision.get("tool_requests"),
                allowed_tools=allowed_tools,
                executed_count=len(tool_observations),
                max_tool_calls=context.limits.max_tool_calls,
            )
            if requested_tools:
                observations = self._execute_model_requested_tools(
                    context=context,
                    requested_tools=requested_tools,
                )
                tool_observations.extend(observations)
                tool_results_summary.extend(
                    {
                        "tool": observation.get("tool"),
                        "status": observation.get("status"),
                        **(
                            {"summary": observation["summary"]}
                            if observation.get("summary") is not None
                            else {}
                        ),
                        **(
                            {"error": observation["error"]}
                            if observation.get("error") is not None
                            else {}
                        ),
                    }
                    for observation in observations
                )
                continue

            final_decision = decision
            break

        status = (
            "model_planned_workflow"
            if is_model_workflow_decision(final_decision)
            else "model_skipped"
            if is_model_skip_decision(final_decision)
            else "model_fallback"
        )
        return ModelPlannerLoopResult(
            status=status,
            iterations=len(message_audit),
            final_decision=final_decision,
            tool_observations=tool_observations,
            tool_results_summary=tool_results_summary,
            message_audit=message_audit,
            error_message=error_message,
        )

    def _execute_model_requested_tools(
        self,
        *,
        context: AgentLoopContext,
        requested_tools: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        """执行模型请求的白名单工具。"""

        runtime = self._resolve_tool_runtime(context)
        if runtime is None:
            return [
                {
                    "tool": request["tool"],
                    "arguments": request.get("arguments") or {},
                    "status": "skipped",
                    "reason": "缺少数据库会话，无法执行模型请求的事实工具。",
                }
                for request in requested_tools
            ]

        observations: list[dict[str, Any]] = []
        for request in requested_tools:
            tool_name = str(request["tool"])
            arguments = dict(request.get("arguments") or {})
            try:
                result = runtime.call(tool_name, **arguments)
                observations.append(
                    {
                        "tool": tool_name,
                        "arguments": arguments,
                        "status": "ok",
                        "summary": summarize_tool_result(result),
                        "reason": request.get("reason"),
                    }
                )
            except Exception as exc:
                observations.append(
                    {
                        "tool": tool_name,
                        "arguments": arguments,
                        "status": "failed",
                        "error": str(exc),
                        "reason": request.get("reason"),
                    }
                )
        return observations

    def _apply_model_loop_result(
        self,
        *,
        fallback_plan: AgentLoopPlan,
        model_loop_result: ModelPlannerLoopResult | None,
        model_decision: dict[str, Any],
    ) -> AgentLoopPlan:
        """把模型最终决策映射为受控 AgentLoopPlan。"""

        if model_loop_result is None:
            return fallback_plan
        final_decision = model_loop_result.final_decision or {}
        if is_model_skip_decision(final_decision):
            return AgentLoopPlan(
                action="skip",
                reason=model_decision["reason"],
                workflow_type=fallback_plan.workflow_type,
                workflow_run_id=fallback_plan.workflow_run_id,
                initial_state=fallback_plan.initial_state,
                tool_calls=tuple(model_loop_result.tool_observations),
            )
        if is_model_workflow_decision(final_decision):
            workflow_type = str(
                final_decision.get("workflow_type") or fallback_plan.workflow_type or ""
            )
            if workflow_type not in SUPPORTED_WORKFLOW_TYPES:
                return fallback_plan
            return AgentLoopPlan(
                action="run_workflow",
                reason=model_decision["reason"],
                workflow_type=workflow_type,
                workflow_run_id=fallback_plan.workflow_run_id
                if workflow_type == fallback_plan.workflow_type
                else build_internal_agent_workflow_run_id(
                    owner_id=str(fallback_plan.initial_state.get("owner_id") or "owner"),
                    workflow_type=workflow_type,
                    agent_task_id=str(
                        (fallback_plan.initial_state.get("trigger_event") or {}).get(
                            "trigger_event_id"
                        )
                        or fallback_plan.workflow_run_id
                        or workflow_type
                    ),
                ),
                initial_state=fallback_plan.initial_state,
                tool_calls=tuple(model_loop_result.tool_observations),
            )
        return fallback_plan

    def _build_model_decision(
        self,
        *,
        context: AgentLoopContext,
        route: dict[str, Any],
        model_ready: bool,
        registry_source: str,
        tool_results_summary: list[dict[str, Any]],
        fallback_plan: AgentLoopPlan,
        prompt_bundle: dict[str, Any],
        model_loop_result: ModelPlannerLoopResult | None = None,
    ) -> dict[str, Any]:
        """构建模型 Planner 审计输出。"""

        event = context.event
        if model_loop_result is not None:
            status = model_loop_result.status
            final_decision = model_loop_result.final_decision or {}
            summary = final_decision.get("summary_zh") or final_decision.get("reasoning_brief_zh")
            if status == "model_planned_workflow":
                reason = str(summary or f"模型 Planner 已决定执行 {fallback_plan.workflow_type}。")
            elif status == "model_skipped":
                reason = str(summary or "模型 Planner 判断本次触发暂不需要调用 Workflow。")
            else:
                reason = (
                    f"模型 Planner 未形成可执行计划，按确定性 fallback 执行 "
                    f"{fallback_plan.workflow_type}。"
                )
        else:
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
            "model_call_executed": bool(model_loop_result and model_loop_result.iterations),
            "registry_source": registry_source,
            "route": route,
            "trigger_event_id": event.trigger_event_id,
            "requested_workflow_type": event.requested_workflow_type,
            "tool_result_count": len(tool_results_summary),
            "fallback_action": fallback_plan.action,
            "fallback_workflow_type": fallback_plan.workflow_type,
            "model_loop_iterations": model_loop_result.iterations if model_loop_result else 0,
            "model_error_message": model_loop_result.error_message if model_loop_result else None,
            "model_final_decision": model_loop_result.final_decision if model_loop_result else None,
            "prompt_model_role": prompt_bundle.get("model_role"),
            "prompt_role_name": prompt_bundle.get("role_name"),
            "prompt_sections": [
                key for key in ("stable", "context", "volatile", "role")
                if prompt_bundle.get(key)
            ],
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


def build_allowed_tool_map(
    tool_calls: tuple[dict[str, object], ...],
) -> dict[str, list[dict[str, object]]]:
    """把 Planner 允许的工具调用转成按工具名分组的白名单。"""

    allowed: dict[str, list[dict[str, object]]] = {}
    for call in tool_calls:
        tool_name = str(call.get("tool") or "")
        if not tool_name:
            continue
        allowed.setdefault(tool_name, []).append(call)
    return allowed


def build_model_planner_messages(
    *,
    prompt_bundle: dict[str, Any],
    prompt_envelope: dict[str, Any],
    fallback_plan: AgentLoopPlan,
    allowed_tool_calls: tuple[dict[str, object], ...],
    tool_observations: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """构建模型 Planner 的 Chat Completions 消息。"""

    allowed_workflows = sorted(SUPPORTED_WORKFLOW_TYPES)
    user_payload = {
        "task": "internal_finance_agent_planning",
        "fallback_plan": {
            "action": fallback_plan.action,
            "workflow_type": fallback_plan.workflow_type,
            "reason": fallback_plan.reason,
        },
        "context_envelope": prompt_envelope,
        "allowed_tool_calls": list(allowed_tool_calls),
        "tool_observations": tool_observations,
        "allowed_workflow_types": allowed_workflows,
        "output_contract": {
            "when_need_more_data": {
                "status": "need_more_data",
                "summary_zh": "为什么需要更多事实",
                "tool_requests": [
                    {
                        "tool": "必须来自 allowed_tool_calls.tool",
                        "arguments": "只能使用对应 allowed_tool_calls 中已有参数或其子集",
                        "reason": "调用原因",
                    }
                ],
                "reasoning_brief_zh": "简短理由",
            },
            "when_ready": {
                "status": "ready",
                "action": "run_workflow",
                "workflow_type": "必须来自 allowed_workflow_types",
                "summary_zh": "中文摘要",
                "confidence": 0.0,
                "risk_flags": [],
                "reasoning_brief_zh": "简短理由",
            },
            "when_skip": {
                "status": "blocked",
                "action": "skip",
                "summary_zh": "中文跳过原因",
                "reasoning_brief_zh": "简短理由",
            },
        },
    }
    return [
        {
            "role": "system",
            "content": (
                f"{prompt_bundle.get('stable', '')}\n"
                "你现在只负责内部 Agent Loop 规划。必须只输出一个 JSON 对象，"
                "不得输出 Markdown、解释性前后缀或隐藏推理链。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True),
        },
    ]


def build_model_response_audit(
    response: ModelClientResponse,
    *,
    iteration: int,
) -> dict[str, Any]:
    """提取单轮模型响应审计摘要。"""

    payload = response.to_audit_dict()
    payload["iteration"] = iteration
    return payload


def normalize_model_tool_requests(
    value: object,
    *,
    allowed_tools: dict[str, list[dict[str, object]]],
    executed_count: int,
    max_tool_calls: int,
) -> tuple[dict[str, Any], ...]:
    """过滤并标准化模型请求的工具调用。"""

    if not isinstance(value, list):
        return ()
    remaining = max(max_tool_calls - executed_count, 0)
    if remaining <= 0:
        return ()
    requests: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool") or "")
        if tool_name not in allowed_tools:
            continue
        arguments = item.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        normalized_arguments = normalize_allowed_tool_arguments(
            tool_name=tool_name,
            requested_arguments=arguments,
            allowed_tools=allowed_tools,
        )
        requests.append(
            {
                "tool": tool_name,
                "arguments": normalized_arguments,
                "reason": item.get("reason"),
            }
        )
        if len(requests) >= remaining:
            break
    return tuple(requests)


def normalize_allowed_tool_arguments(
    *,
    tool_name: str,
    requested_arguments: dict[str, Any],
    allowed_tools: dict[str, list[dict[str, object]]],
) -> dict[str, Any]:
    """把模型参数约束回 Planner 提供的白名单参数集合。"""

    allowed_variants = allowed_tools.get(tool_name) or []
    if not allowed_variants:
        return {}
    base = {key: value for key, value in allowed_variants[0].items() if key != "tool"}
    normalized = dict(base)
    for key, value in requested_arguments.items():
        if key in base:
            normalized[key] = value
    return normalized


def is_model_workflow_decision(decision: dict[str, Any] | None) -> bool:
    """判断模型是否选择运行受支持 Workflow。"""

    if not isinstance(decision, dict):
        return False
    action = str(decision.get("action") or "").strip()
    status = str(decision.get("status") or "").strip()
    workflow_type = str(decision.get("workflow_type") or "").strip()
    return (
        status in {"ready", "run_workflow", "completed"}
        and action in {"run_workflow", "workflow", ""}
        and workflow_type in SUPPORTED_WORKFLOW_TYPES
    )


def is_model_skip_decision(decision: dict[str, Any] | None) -> bool:
    """判断模型是否明确选择跳过。"""

    if not isinstance(decision, dict):
        return False
    action = str(decision.get("action") or "").strip()
    status = str(decision.get("status") or "").strip()
    return action == "skip" or status in {"blocked", "skip", "skipped"}


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
                {
                    "tool": "memory.get_asset_memory_context",
                    "owner_id": event.owner_id,
                    "asset_id": event.asset_id,
                    "query": build_memory_query_for_event(event),
                    "limit": 8,
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


def build_planner_prompt_envelope(
    *,
    event: object,
    workflow_type: str,
    tool_calls: tuple[dict[str, object], ...],
    tool_results_summary: list[dict[str, Any]],
) -> dict[str, Any]:
    """为内部模型 Planner 构造轻量 Prompt Envelope。"""

    asset_ids = [event.asset_id] if event.asset_id else []
    asset_contexts = {
        event.asset_id: {
            "profile": {
                "asset_id": event.asset_id,
                "market": infer_event_market_type(event),
            },
            "memory": {
                "memories": extract_tool_memory_items(tool_results_summary),
            },
            "signal_risk": {
                "risks": extract_tool_risk_items(tool_results_summary),
            },
        }
    } if event.asset_id else {}
    return build_workflow_context_envelope(
        workflow_type=workflow_type,
        market_type=infer_event_market_type(event),
        asset_ids=asset_ids,
        asset_contexts=asset_contexts,
        portfolio_context={"portfolio_id": event.portfolio_id}
        if event.portfolio_id
        else None,
        watchlist_context={"watchlist_id": event.watchlist_id}
        if event.watchlist_id
        else None,
        recommendation_context={"run_id": event.recommendation_run_id}
        if event.recommendation_run_id
        else None,
        trigger_event={
            "trigger_event_id": event.trigger_event_id,
            "trigger_type": event.trigger_type,
            "trigger_ref": event.trigger_ref,
            "severity": event.severity,
            "requested_workflow_type": event.requested_workflow_type,
            "reason": (event.payload or {}).get("reason"),
        },
        available_tools=[
            str(call.get("tool")) for call in tool_calls if call.get("tool")
        ],
        memory_summary={
            "memory_count": count_summary_items(tool_results_summary, "memories"),
            "items": extract_tool_memory_items(tool_results_summary),
        },
        risk_summary={
            "risk_count": count_summary_items(tool_results_summary, "risks"),
            "high_risk_count": count_high_risk_summary_items(tool_results_summary),
            "items": extract_tool_risk_items(tool_results_summary),
        },
    ).to_dict()


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
                    "query": build_memory_query_for_event(event),
                },
            )
        )
    return tuple(calls)


def infer_event_market_type(event: object) -> str:
    """从触发事件推断市场类型。"""

    payload = event.payload or {}
    market = payload.get("market")
    if isinstance(market, str) and market.strip():
        return market.strip()
    return "ashare"


def build_memory_query_for_event(event: object) -> str:
    """为触发事件构造 Finance Memory 召回查询。"""

    payload = event.payload or {}
    parts = [
        str(event.trigger_type or ""),
        str(event.requested_workflow_type or ""),
        str(payload.get("reason") or ""),
        str(payload.get("summary") or ""),
        str(payload.get("trigger_condition") or ""),
        str(payload.get("suggested_action") or ""),
        str(event.asset_id or ""),
    ]
    query = " ".join(part for part in parts if part.strip())
    return query or "候选池 持续观察 买入 卖出 换股 风险 复盘"


def extract_tool_memory_items(tool_results_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从工具摘要中提取少量记忆条目，供 Prompt volatile 层使用。"""

    items: list[dict[str, Any]] = []
    for result in tool_results_summary:
        summary = result.get("summary") or {}
        for key in ("memories", "similar_memories", "timeline", "items"):
            memories = summary.get(key) or {}
            sample = memories.get("sample") if isinstance(memories, dict) else None
            if isinstance(sample, list):
                for item in sample:
                    if isinstance(item, dict):
                        items.append(item)
    return items[:5]


def extract_tool_risk_items(tool_results_summary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从工具摘要中提取少量风险条目，供 Prompt volatile 层使用。"""

    items: list[dict[str, Any]] = []
    for result in tool_results_summary:
        summary = result.get("summary") or {}
        risks = summary.get("risks") or summary.get("risk_items") or {}
        sample = risks.get("sample") if isinstance(risks, dict) else None
        if isinstance(sample, list):
            for item in sample:
                if isinstance(item, dict):
                    items.append(item)
    return items[:5]


def count_summary_items(tool_results_summary: list[dict[str, Any]], key: str) -> int:
    """统计工具摘要中的列表数量。"""

    total = 0
    for result in tool_results_summary:
        summary = result.get("summary") or {}
        value = summary.get(key)
        if isinstance(value, dict):
            total += int(value.get("count") or 0)
        if key == "memories":
            for alias in ("similar_memories", "timeline"):
                alias_value = summary.get(alias)
                if isinstance(alias_value, dict):
                    total += int(alias_value.get("count") or 0)
    return total


def count_high_risk_summary_items(tool_results_summary: list[dict[str, Any]]) -> int:
    """统计工具摘要样本中的高风险数量。"""

    return sum(
        1
        for item in extract_tool_risk_items(tool_results_summary)
        if item.get("severity") in {"high", "critical"}
    )


def dedupe_tool_calls(calls: list[dict[str, object]]) -> list[dict[str, object]]:
    """按工具名和参数对工具计划去重。"""

    seen: set[tuple[tuple[str, str], ...]] = set()
    result: list[dict[str, object]] = []
    for call in calls:
        key = tuple(
            sorted((str(item_key), str(item_value)) for item_key, item_value in call.items())
        )
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
