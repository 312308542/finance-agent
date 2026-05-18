"""Agent Workflow 审计应用服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import AgentWorkflowEventORM, AgentWorkflowRunORM
from finance_agent.storage.repositories import WorkflowAuditRepository

JsonDict = dict[str, Any]


class WorkflowService:
    """记录上层主 Agent 调用底层金融团队 Workflow 的过程。"""

    def __init__(self, session: Session) -> None:
        self.repository = WorkflowAuditRepository(session)

    def start_run(
        self,
        *,
        workflow_run_id: str,
        owner_id: str,
        workflow_type: str,
        trigger_type: str,
        started_at: datetime,
        trigger_ref: str | None = None,
        input_ref: str | None = None,
        payload: JsonDict | None = None,
    ) -> AgentWorkflowRunORM:
        """开始一次 Workflow 运行。"""

        return self.repository.upsert_run(
            workflow_run_id=workflow_run_id,
            owner_id=owner_id,
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            status="running",
            started_at=started_at,
            input_ref=input_ref,
            payload=payload,
        )

    def finish_run(
        self,
        *,
        workflow_run_id: str,
        owner_id: str,
        workflow_type: str,
        trigger_type: str,
        started_at: datetime,
        status: str,
        finished_at: datetime,
        trigger_ref: str | None = None,
        input_ref: str | None = None,
        output_ref: str | None = None,
        payload: JsonDict | None = None,
    ) -> AgentWorkflowRunORM:
        """结束一次 Workflow 运行。"""

        return self.repository.upsert_run(
            workflow_run_id=workflow_run_id,
            owner_id=owner_id,
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            input_ref=input_ref,
            output_ref=output_ref,
            payload=payload,
        )

    def record_event(
        self,
        *,
        workflow_event_id: str,
        workflow_run_id: str,
        event_type: str,
        message: str,
        agent_name: str | None = None,
        evidence_ids: list[str] | None = None,
        created_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> AgentWorkflowEventORM:
        """记录 Workflow 节点事件或工具调用摘要。"""

        return self.repository.insert_event(
            workflow_event_id=workflow_event_id,
            workflow_run_id=workflow_run_id,
            event_type=event_type,
            agent_name=agent_name,
            message=message,
            evidence_ids=evidence_ids,
            created_at=created_at,
            payload=payload,
        )
