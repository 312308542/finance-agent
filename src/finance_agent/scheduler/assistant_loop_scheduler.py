"""私人金融助手触发评估和内部 Agent Loop 常驻调度器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import sleep
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.agents.loop import InternalFinanceAgentLoopRunner, ModelFinanceAgentPlanner
from finance_agent.agents.loop.state import AgentLoopRunResult
from finance_agent.triggers import (
    AgentWakeupDispatchResult,
    TriggerEvaluationRequest,
    TriggerEvaluationResult,
    TriggerService,
)

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AssistantLoopSchedulerConfig:
    """内部金融助手常驻调度配置。"""

    owner_id: str | None = None
    interval_seconds: float = 5.0
    trigger_limit: int = 20
    agent_limit: int = 20
    max_cycles: int | None = None
    run_agent_once: bool = True
    use_graph: bool = True
    agent_runtime: str = "internal_agent_loop"


@dataclass(frozen=True)
class AssistantLoopSchedulerCycleResult:
    """一次调度轮询结果。"""

    cycle_no: int
    evaluation: TriggerEvaluationResult
    dispatch: AgentWakeupDispatchResult
    agent_result: AgentLoopRunResult | None = None

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        agent_payload = self.agent_result.to_dict() if self.agent_result else None
        return {
            "cycle_no": self.cycle_no,
            "created_count": len(self.evaluation.created_events),
            "dispatched_count": len(self.dispatch.dispatched_events),
            "skipped_dispatch_count": len(self.dispatch.skipped_events),
            "agent_processed_count": len(self.agent_result.processed)
            if self.agent_result
            else 0,
            "agent_failed_count": len(self.agent_result.failed) if self.agent_result else 0,
            "evaluation": self.evaluation.to_dict(),
            "dispatch": self.dispatch.to_dict(),
            "agent_result": agent_payload,
        }


@dataclass(frozen=True)
class AssistantLoopSchedulerResult:
    """常驻调度汇总结果。"""

    cycle_results: tuple[AssistantLoopSchedulerCycleResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "cycles": len(self.cycle_results),
            "created_count": sum(
                len(cycle.evaluation.created_events) for cycle in self.cycle_results
            ),
            "dispatched_count": sum(
                len(cycle.dispatch.dispatched_events) for cycle in self.cycle_results
            ),
            "agent_processed_count": sum(
                len(cycle.agent_result.processed)
                for cycle in self.cycle_results
                if cycle.agent_result
            ),
            "agent_failed_count": sum(
                len(cycle.agent_result.failed)
                for cycle in self.cycle_results
                if cycle.agent_result
            ),
            "cycle_results": [cycle.to_dict() for cycle in self.cycle_results],
        }


class AssistantLoopScheduler:
    """把触发评估、事件派发和内部 Agent Loop 串成常驻服务。"""

    def __init__(
        self,
        *,
        session: Session,
        config: AssistantLoopSchedulerConfig | None = None,
        trigger_service: TriggerService | None = None,
        runner: InternalFinanceAgentLoopRunner | None = None,
    ) -> None:
        self.session = session
        self.config = config or AssistantLoopSchedulerConfig()
        self.trigger_service = trigger_service or TriggerService(session)
        self.runner = runner or InternalFinanceAgentLoopRunner(
            session,
            planner=ModelFinanceAgentPlanner(),
        )

    def run_once(
        self,
        *,
        request: TriggerEvaluationRequest,
        cycle_no: int = 1,
    ) -> AssistantLoopSchedulerCycleResult:
        """执行一轮触发评估、派发和内部 Agent 消费。"""

        normalized_request = self._normalize_request(request)
        evaluation = self.trigger_service.evaluate(normalized_request)
        dispatch = self.trigger_service.dispatch_pending(
            owner_id=normalized_request.owner_id,
            limit=max(self.config.trigger_limit, len(evaluation.created_events), 1),
            as_of=normalized_request.as_of,
            agent_runtime=self.config.agent_runtime,
        )
        agent_result = None
        if self.config.run_agent_once:
            agent_result = self.runner.run_once(
                owner_id=normalized_request.owner_id,
                limit=self.config.agent_limit,
                as_of=normalized_request.as_of,
                use_graph=self.config.use_graph,
            )
        return AssistantLoopSchedulerCycleResult(
            cycle_no=cycle_no,
            evaluation=evaluation,
            dispatch=dispatch,
            agent_result=agent_result,
        )

    def run_loop(self, *, request: TriggerEvaluationRequest) -> AssistantLoopSchedulerResult:
        """持续轮询；`max_cycles=None` 时用于长期服务化运行。"""

        cycle_results: list[AssistantLoopSchedulerCycleResult] = []
        cycle_no = 0
        while self.config.max_cycles is None or cycle_no < self.config.max_cycles:
            cycle_no += 1
            cycle_request = self._normalize_request(
                request,
                as_of=request.as_of if self.config.max_cycles is not None else datetime.now(UTC),
            )
            cycle_results.append(self.run_once(request=cycle_request, cycle_no=cycle_no))
            if self.config.max_cycles is not None and cycle_no >= self.config.max_cycles:
                break
            sleep(max(self.config.interval_seconds, 0))
        return AssistantLoopSchedulerResult(cycle_results=tuple(cycle_results))

    def _normalize_request(
        self,
        request: TriggerEvaluationRequest,
        *,
        as_of: datetime | None = None,
    ) -> TriggerEvaluationRequest:
        """合并 Scheduler 配置中的默认 owner 和运行时间。"""

        return TriggerEvaluationRequest(
            owner_id=self.config.owner_id or request.owner_id,
            as_of=as_of or request.as_of,
            portfolio_id=request.portfolio_id,
            watchlist_id=request.watchlist_id,
            recommendation_run_id=request.recommendation_run_id,
            horizon=request.horizon,
            timeframe=request.timeframe,
            since_minutes=request.since_minutes,
            cooldown_minutes=request.cooldown_minutes,
            recommendation_limit=request.recommendation_limit,
            drawdown_threshold=request.drawdown_threshold,
        )
