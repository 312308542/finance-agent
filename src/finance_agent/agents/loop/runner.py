"""内部金融 Agent Loop 运行器。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.agents.loop.planner import InternalFinanceAgentPlanner
from finance_agent.agents.loop.state import (
    AgentLoopContext,
    AgentLoopLimits,
    AgentLoopRunResult,
    AgentLoopTaskResult,
)
from finance_agent.storage.orm import AssistantTriggerEventORM
from finance_agent.storage.repositories import AssistantTriggerRepository


class InternalFinanceAgentLoopRunner:
    """项目内受控金融 Agent Loop。

    它消费触发层派发出的 Agent 唤醒事件，再由规划器决定是否调用底层金融团队
    Workflow。当前实现是确定性规划器，后续可以把 planner 替换为 LangGraph
    loop，但 runner 的幂等、边界和审计语义保持不变。
    """

    def __init__(
        self,
        session: Session,
        *,
        planner: InternalFinanceAgentPlanner | None = None,
        limits: AgentLoopLimits | None = None,
    ) -> None:
        self.session = session
        self.triggers = AssistantTriggerRepository(session)
        self.interface = FinanceAgentInterface(session)
        self.planner = planner or InternalFinanceAgentPlanner()
        self.limits = limits or AgentLoopLimits()

    def run_once(
        self,
        *,
        owner_id: str | None = None,
        limit: int = 20,
        as_of: datetime | None = None,
    ) -> AgentLoopRunResult:
        """处理一批已派发的 Agent 唤醒事件。"""

        run_at = as_of or datetime.now(UTC)
        processed: list[AgentLoopTaskResult] = []
        skipped: list[AgentLoopTaskResult] = []
        failed: list[AgentLoopTaskResult] = []
        events = self.triggers.list_agent_wakeup_events(owner_id=owner_id, limit=limit)
        for event in events:
            result = self._handle_event(event=event, as_of=run_at)
            if result.status == "workflow_completed":
                processed.append(result)
            elif result.status == "failed":
                failed.append(result)
            else:
                skipped.append(result)
        return AgentLoopRunResult(
            processed=tuple(processed),
            skipped=tuple(skipped),
            failed=tuple(failed),
        )

    def run_task(
        self,
        *,
        agent_task_id: str,
        as_of: datetime | None = None,
    ) -> AgentLoopRunResult:
        """按 Agent 任务 ID 处理单个唤醒事件。"""

        run_at = as_of or datetime.now(UTC)
        event = self.triggers.get_trigger_event_by_agent_task_id(agent_task_id)
        if event is None:
            return AgentLoopRunResult(
                skipped=(
                    AgentLoopTaskResult(
                        agent_task_id=agent_task_id,
                        trigger_event_id="",
                        status="skipped",
                        action="skip",
                        reason="未找到 Agent 任务对应的触发事件。",
                    ),
                )
            )
        result = self._handle_event(event=event, as_of=run_at)
        if result.status == "workflow_completed":
            return AgentLoopRunResult(processed=(result,))
        if result.status == "failed":
            return AgentLoopRunResult(failed=(result,))
        return AgentLoopRunResult(skipped=(result,))

    def _handle_event(
        self,
        *,
        event: AssistantTriggerEventORM,
        as_of: datetime,
    ) -> AgentLoopTaskResult:
        agent_task_id = event.agent_task_id or event.trigger_event_id
        try:
            context = AgentLoopContext(event=event, as_of=as_of, limits=self.limits)
            plan = self.planner.build_plan(context)
            if plan.action != "run_workflow":
                self.triggers.mark_agent_loop_skipped(
                    trigger_event_id=event.trigger_event_id,
                    skipped_at=as_of,
                    reason=plan.reason,
                    payload={
                        "agent_plan": plan.action,
                        "planned_tool_calls": list(plan.tool_calls),
                    },
                )
                return AgentLoopTaskResult(
                    agent_task_id=agent_task_id,
                    trigger_event_id=event.trigger_event_id,
                    status="skipped",
                    action=plan.action,
                    reason=plan.reason,
                    workflow_type=plan.workflow_type,
                    workflow_run_id=plan.workflow_run_id,
                    tool_calls=plan.tool_calls,
                    step_count=1,
                )

            if not plan.workflow_type or not plan.workflow_run_id:
                raise ValueError("内部 Agent 计划缺少 workflow_type 或 workflow_run_id。")

            workflow_result = self.interface.run_workflow(
                workflow_type=plan.workflow_type,
                owner_id=event.owner_id,
                workflow_run_id=plan.workflow_run_id,
                trigger_type=f"agent_loop:{event.trigger_type}",
                trigger_ref=event.trigger_event_id,
                started_at=as_of,
                initial_state=plan.initial_state,
                portfolio_id=event.portfolio_id,
                watchlist_id=event.watchlist_id,
                recommendation_run_id=event.recommendation_run_id,
                asset_id=event.asset_id,
                asset_ids=[event.asset_id] if event.asset_id else [],
                horizon=plan.initial_state.get("horizon", "swing"),
                timeframe=plan.initial_state.get("timeframe", "1d"),
                recommendation_limit=int(plan.initial_state.get("recommendation_limit", 20)),
            )
            workflow_data = workflow_result.to_dict()["data"]
            workflow_run_id = workflow_data["workflow_run_id"]
            self.triggers.mark_agent_loop_completed(
                trigger_event_id=event.trigger_event_id,
                workflow_run_id=workflow_run_id,
                completed_at=as_of,
                payload={
                    "agent_plan": plan.action,
                    "agent_reason": plan.reason,
                    "planned_tool_calls": list(plan.tool_calls),
                    "workflow_type": plan.workflow_type,
                    "workflow_report": workflow_data.get("report"),
                },
            )
            return AgentLoopTaskResult(
                agent_task_id=agent_task_id,
                trigger_event_id=event.trigger_event_id,
                status="workflow_completed",
                action=plan.action,
                reason=plan.reason,
                workflow_type=plan.workflow_type,
                workflow_run_id=workflow_run_id,
                tool_calls=plan.tool_calls,
                step_count=3,
            )
        except Exception as exc:
            self.triggers.mark_agent_loop_failed(
                trigger_event_id=event.trigger_event_id,
                failed_at=as_of,
                error_message=str(exc),
            )
            return AgentLoopTaskResult(
                agent_task_id=agent_task_id,
                trigger_event_id=event.trigger_event_id,
                status="failed",
                action="run_workflow",
                reason="内部 Agent Loop 处理失败。",
                error_message=str(exc),
                step_count=1,
            )
