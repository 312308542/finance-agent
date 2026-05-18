"""内部金融 Agent Loop 的状态对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from finance_agent.storage.orm import AssistantTriggerEventORM

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AgentLoopLimits:
    """内部 Agent Loop 的硬性运行边界。"""

    max_steps: int = 6
    max_tool_calls: int = 8
    max_workflow_calls: int = 1


@dataclass(frozen=True)
class AgentLoopPlan:
    """内部 Agent 对单个唤醒事件形成的执行计划。"""

    action: str
    reason: str
    workflow_type: str | None = None
    workflow_run_id: str | None = None
    initial_state: JsonDict = field(default_factory=dict)
    tool_calls: tuple[JsonDict, ...] = ()


@dataclass(frozen=True)
class AgentLoopTaskResult:
    """单个 Agent 唤醒任务的处理结果。"""

    agent_task_id: str
    trigger_event_id: str
    status: str
    action: str
    reason: str
    workflow_type: str | None = None
    workflow_run_id: str | None = None
    tool_calls: tuple[JsonDict, ...] = ()
    step_count: int = 0
    error_message: str | None = None

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        result: JsonDict = {
            "agent_task_id": self.agent_task_id,
            "trigger_event_id": self.trigger_event_id,
            "status": self.status,
            "action": self.action,
            "reason": self.reason,
            "workflow_type": self.workflow_type,
            "workflow_run_id": self.workflow_run_id,
            "tool_calls": list(self.tool_calls),
            "step_count": self.step_count,
        }
        if self.error_message:
            result["error_message"] = self.error_message
        return result


@dataclass(frozen=True)
class AgentLoopRunResult:
    """一次内部 Agent Loop 批处理结果。"""

    processed: tuple[AgentLoopTaskResult, ...] = ()
    skipped: tuple[AgentLoopTaskResult, ...] = ()
    failed: tuple[AgentLoopTaskResult, ...] = ()

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "processed_count": len(self.processed),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
            "runs": [item.to_dict() for item in self.processed],
            "skipped": [item.to_dict() for item in self.skipped],
            "failed": [item.to_dict() for item in self.failed],
        }


@dataclass(frozen=True)
class AgentLoopDaemonResult:
    """内部 Agent Loop 常驻轮询的一次汇总结果。"""

    iterations: int
    processed: tuple[AgentLoopTaskResult, ...] = ()
    skipped: tuple[AgentLoopTaskResult, ...] = ()
    failed: tuple[AgentLoopTaskResult, ...] = ()

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "iterations": self.iterations,
            "processed_count": len(self.processed),
            "skipped_count": len(self.skipped),
            "failed_count": len(self.failed),
            "runs": [item.to_dict() for item in self.processed],
            "skipped": [item.to_dict() for item in self.skipped],
            "failed": [item.to_dict() for item in self.failed],
        }


@dataclass(frozen=True)
class AgentLoopContext:
    """内部 Agent Loop 当前任务上下文。"""

    event: AssistantTriggerEventORM
    as_of: datetime
    limits: AgentLoopLimits
