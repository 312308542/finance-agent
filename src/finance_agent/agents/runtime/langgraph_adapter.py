"""LangGraph Workflow 审计适配层。

LangGraph 负责图编排和状态流转；本模块只负责把节点运行摘要写入本项目的
Workflow 审计表，避免自研 AI Workflow 框架。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.application import WorkflowService
from finance_agent.storage.orm import AgentWorkflowEventORM

WorkflowState = dict[str, Any]


@dataclass(frozen=True)
class WorkflowNodeEvent:
    """LangGraph 节点审计事件。"""

    name: str
    output: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()
    message: str | None = None


class LangGraphWorkflowAdapter:
    """连接 LangGraph 工作流和本项目审计落库的适配器。"""

    def __init__(self, session: Session) -> None:
        self.audit = WorkflowService(session)

    def record_completed_graph(
        self,
        *,
        workflow_run_id: str,
        owner_id: str,
        workflow_type: str,
        trigger_type: str,
        started_at: datetime,
        node_events: tuple[WorkflowNodeEvent, ...],
        initial_state: WorkflowState,
        final_state: WorkflowState,
        trigger_ref: str | None = None,
        input_ref: str | None = None,
        output_ref: str | None = None,
        finished_at: datetime | None = None,
    ) -> WorkflowState:
        """记录一次已经由 LangGraph 执行完成的工作流。"""

        context_envelope = extract_context_envelope(initial_state=initial_state, final_state=final_state)
        audit_payload = {
            "engine": "langgraph",
            "node_count": len(node_events),
            "initial_keys": sorted(initial_state),
        }
        if context_envelope is not None:
            audit_payload["context_envelope"] = context_envelope
            audit_payload["context_envelope_summary"] = summarize_context_envelope(
                context_envelope
            )
        self.audit.start_run(
            workflow_run_id=workflow_run_id,
            owner_id=owner_id,
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            started_at=started_at,
            input_ref=input_ref,
            payload=audit_payload,
        )
        for index, event in enumerate(node_events, start=1):
            self.audit.record_event(
                workflow_event_id=f"{workflow_run_id}:node:{index}:{event.name}",
                workflow_run_id=workflow_run_id,
                event_type=classify_workflow_event_type(event.name),
                agent_name=event.name,
                evidence_ids=list(event.evidence_ids),
                message=event.message or f"LangGraph 节点已完成：{event.name}",
                created_at=started_at,
                payload={
                    "engine": "langgraph",
                    "node": index,
                    "output_keys": sorted(event.output),
                    "output": event.output,
                },
            )
        self.audit.finish_run(
            workflow_run_id=workflow_run_id,
            owner_id=owner_id,
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            started_at=started_at,
            finished_at=finished_at or started_at,
            status="succeeded",
            input_ref=input_ref,
            output_ref=output_ref,
            payload={
                "engine": "langgraph",
                "final_keys": sorted(final_state),
                **(
                    {
                        "context_envelope": context_envelope,
                        "context_envelope_summary": summarize_context_envelope(
                            context_envelope
                        ),
                    }
                    if context_envelope is not None
                    else {}
                ),
            },
        )
        return final_state

    def record_failed_graph(
        self,
        *,
        workflow_run_id: str,
        owner_id: str,
        workflow_type: str,
        trigger_type: str,
        started_at: datetime,
        error_message: str,
        trigger_ref: str | None = None,
        input_ref: str | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """记录一次 LangGraph 工作流失败。"""

        self.audit.finish_run(
            workflow_run_id=workflow_run_id,
            owner_id=owner_id,
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            started_at=started_at,
            finished_at=finished_at or started_at,
            status="failed",
            input_ref=input_ref,
            payload={"engine": "langgraph", "error_message": error_message},
        )

    def list_events(self, workflow_run_id: str) -> tuple[AgentWorkflowEventORM, ...]:
        """查询一次 Workflow 的审计事件。"""

        return tuple(self.audit.repository.list_events(workflow_run_id))


def classify_workflow_event_type(name: str) -> str:
    """根据节点名归类审计事件类型。"""

    if name.startswith("roundtable:"):
        return "roundtable_opinion"
    if name.startswith("model_route:"):
        return "model_route"
    if name.startswith("model_review:"):
        return "model_review"
    if name.startswith("high_risk_review:") or name == "high_risk_review":
        return "high_risk_review"
    if name == "report_draft":
        return "report_draft"
    return "workflow_node_completed"


def extract_context_envelope(
    *,
    initial_state: WorkflowState,
    final_state: WorkflowState,
) -> dict[str, Any] | None:
    """从工作流状态中提取共享上下文封装。"""

    context_envelope = final_state.get("context_envelope")
    if isinstance(context_envelope, dict):
        return context_envelope
    context_envelope = initial_state.get("context_envelope")
    if isinstance(context_envelope, dict):
        return context_envelope
    return None


def summarize_context_envelope(context_envelope: dict[str, Any]) -> dict[str, Any]:
    """提取适合写入审计表的上下文摘要。"""

    stable = context_envelope.get("stable") or {}
    context = context_envelope.get("context") or {}
    volatile = context_envelope.get("volatile") or {}
    role_views = context_envelope.get("role_views") or {}
    audit = context_envelope.get("audit") or {}
    return {
        "version": context_envelope.get("version"),
        "workflow_type": context_envelope.get("workflow_type"),
        "market_type": context_envelope.get("market_type"),
        "asset_count": len(context.get("asset_ids") or []),
        "role_view_count": len(role_views),
        "stable_keys": sorted(stable),
        "context_keys": sorted(context),
        "volatile_keys": sorted(volatile),
        "audit_keys": sorted(audit),
    }
