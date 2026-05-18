"""内部金融 Agent Loop 的 LangGraph 图。

图只负责把一次已派发 Agent 唤醒事件拆成可审计步骤：加载任务、规划、执行
Workflow、持久化结果。触发层仍然只生成/派发事件，不直接运行 Workflow。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.agents.loop.planner import InternalFinanceAgentPlanner
from finance_agent.agents.loop.state import (
    AgentLoopContext,
    AgentLoopLimits,
    AgentLoopPlan,
    AgentLoopTaskResult,
)
from finance_agent.storage.orm import AssistantTriggerEventORM
from finance_agent.storage.repositories import AssistantTriggerRepository


class InternalAgentLoopGraphUnavailable(RuntimeError):
    """内部 Agent Loop 图运行时不可用。"""


def build_internal_agent_loop_graph() -> Any:
    """构建内部 Agent Loop 的 LangGraph 入口。"""

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise InternalAgentLoopGraphUnavailable(
            "缺少 langgraph 依赖。请先安装项目依赖："
            ".venv\\Scripts\\python.exe -m pip install langgraph"
        ) from exc

    graph = StateGraph(dict)

    def load_task(state: dict[str, Any]) -> dict[str, Any]:
        event = _resolve_event(state)
        return {
            **state,
            "event": event,
            "agent_task_id": event.agent_task_id or event.trigger_event_id,
            "node_trace": [*state.get("node_trace", []), "load_task"],
        }

    def plan(state: dict[str, Any]) -> dict[str, Any]:
        planner = state.get("planner") or InternalFinanceAgentPlanner()
        context = AgentLoopContext(
            event=state["event"],
            as_of=state["as_of"],
            limits=state.get("limits") or AgentLoopLimits(),
        )
        agent_plan = planner.build_plan(context)
        return {
            **state,
            "plan": agent_plan,
            "node_trace": [*state.get("node_trace", []), "plan"],
        }

    def execute_workflow(state: dict[str, Any]) -> dict[str, Any]:
        agent_plan: AgentLoopPlan = state["plan"]
        if agent_plan.action != "run_workflow":
            return {
                **state,
                "workflow_data": None,
                "node_trace": [*state.get("node_trace", []), "execute_workflow"],
            }
        if not agent_plan.workflow_type or not agent_plan.workflow_run_id:
            raise ValueError("内部 Agent 计划缺少 workflow_type 或 workflow_run_id。")

        event: AssistantTriggerEventORM = state["event"]
        interface = state.get("interface") or FinanceAgentInterface(_resolve_session(state))
        workflow_result = interface.run_workflow(
            workflow_type=agent_plan.workflow_type,
            owner_id=event.owner_id,
            workflow_run_id=agent_plan.workflow_run_id,
            trigger_type=f"agent_loop:{event.trigger_type}",
            trigger_ref=event.trigger_event_id,
            started_at=state["as_of"],
            initial_state=agent_plan.initial_state,
            portfolio_id=event.portfolio_id,
            watchlist_id=event.watchlist_id,
            recommendation_run_id=event.recommendation_run_id,
            asset_id=event.asset_id,
            asset_ids=[event.asset_id] if event.asset_id else [],
            horizon=agent_plan.initial_state.get("horizon", "swing"),
            timeframe=agent_plan.initial_state.get("timeframe", "1d"),
            recommendation_limit=int(agent_plan.initial_state.get("recommendation_limit", 20)),
        )
        return {
            **state,
            "workflow_data": workflow_result.to_dict()["data"],
            "node_trace": [*state.get("node_trace", []), "execute_workflow"],
        }

    def persist_result(state: dict[str, Any]) -> dict[str, Any]:
        event: AssistantTriggerEventORM = state["event"]
        agent_plan: AgentLoopPlan = state["plan"]
        triggers = state.get("triggers") or AssistantTriggerRepository(_resolve_session(state))
        node_trace = [*state.get("node_trace", []), "persist_result"]
        if agent_plan.action != "run_workflow":
            triggers.mark_agent_loop_skipped(
                trigger_event_id=event.trigger_event_id,
                skipped_at=state["as_of"],
                reason=agent_plan.reason,
                payload={
                    "agent_plan": agent_plan.action,
                    "planned_tool_calls": list(agent_plan.tool_calls),
                    "agent_node_trace": node_trace,
                },
            )
            task_result = AgentLoopTaskResult(
                agent_task_id=state["agent_task_id"],
                trigger_event_id=event.trigger_event_id,
                status="skipped",
                action=agent_plan.action,
                reason=agent_plan.reason,
                workflow_type=agent_plan.workflow_type,
                workflow_run_id=agent_plan.workflow_run_id,
                tool_calls=agent_plan.tool_calls,
                step_count=len(node_trace),
            )
        else:
            workflow_data = state["workflow_data"]
            workflow_run_id = workflow_data["workflow_run_id"]
            triggers.mark_agent_loop_completed(
                trigger_event_id=event.trigger_event_id,
                workflow_run_id=workflow_run_id,
                completed_at=state["as_of"],
                payload={
                    "agent_plan": agent_plan.action,
                    "agent_reason": agent_plan.reason,
                    "planned_tool_calls": list(agent_plan.tool_calls),
                    "workflow_type": agent_plan.workflow_type,
                    "workflow_report": workflow_data.get("report"),
                    "agent_node_trace": node_trace,
                },
            )
            task_result = AgentLoopTaskResult(
                agent_task_id=state["agent_task_id"],
                trigger_event_id=event.trigger_event_id,
                status="workflow_completed",
                action=agent_plan.action,
                reason=agent_plan.reason,
                workflow_type=agent_plan.workflow_type,
                workflow_run_id=workflow_run_id,
                tool_calls=agent_plan.tool_calls,
                step_count=len(node_trace),
            )
        return {
            **state,
            "task_result": task_result,
            "node_trace": node_trace,
        }

    graph.add_node("load_task", load_task)
    graph.add_node("plan", plan)
    graph.add_node("execute_workflow", execute_workflow)
    graph.add_node("persist_result", persist_result)
    graph.add_edge(START, "load_task")
    graph.add_edge("load_task", "plan")
    graph.add_edge("plan", "execute_workflow")
    graph.add_edge("execute_workflow", "persist_result")
    graph.add_edge("persist_result", END)
    return graph.compile()


def _resolve_session(state: dict[str, Any]) -> Session:
    """从图状态中读取数据库会话。"""

    session = state.get("session")
    if session is None:
        raise ValueError("内部 Agent Loop 图缺少 session。")
    return session


def _resolve_event(state: dict[str, Any]) -> AssistantTriggerEventORM:
    """从图状态中解析触发事件。"""

    event = state.get("event")
    if event is not None:
        return event

    agent_task_id = state.get("agent_task_id")
    if not agent_task_id:
        raise ValueError("内部 Agent Loop 图缺少 event 或 agent_task_id。")
    triggers = state.get("triggers") or AssistantTriggerRepository(_resolve_session(state))
    resolved = triggers.get_trigger_event_by_agent_task_id(str(agent_task_id))
    if resolved is None:
        raise ValueError(f"未找到 Agent 任务对应的触发事件：{agent_task_id}")
    return resolved
