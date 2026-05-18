"""Finance Memory 和决策日志应用服务。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    AssistantMemoryORM,
    DecisionLogORM,
    FinancialMemoryEdgeORM,
    MonitoringAlertORM,
    ReviewTaskORM,
)
from finance_agent.storage.repositories import DecisionLogRepository, MemoryRepository

JsonDict = dict[str, Any]


class MemoryService:
    """金融业务记忆服务。

    Finance Memory 只保存可审计的金融业务上下文，不保存 Hermes-agent 的通用对话记忆。
    """

    def __init__(self, session: Session) -> None:
        self.decisions = DecisionLogRepository(session)
        self.memories = MemoryRepository(session)

    def record_alert(
        self,
        *,
        alert_id: str,
        owner_id: str,
        alert_type: str,
        severity: str,
        triggered_by: str,
        trigger_condition: str,
        as_of: datetime,
        portfolio_id: str | None = None,
        asset_id: str | None = None,
        current_value: Decimal | None = None,
        threshold_value: Decimal | None = None,
        status: str = "triggered",
        payload: JsonDict | None = None,
    ) -> MonitoringAlertORM:
        """记录一条监控提醒。"""

        return self.decisions.insert_monitoring_alert(
            alert_id=alert_id,
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            alert_type=alert_type,
            severity=severity,
            triggered_by=triggered_by,
            trigger_condition=trigger_condition,
            current_value=current_value,
            threshold_value=threshold_value,
            status=status,
            as_of=as_of,
            payload=payload,
        )

    def record_decision(
        self,
        *,
        decision_id: str,
        owner_id: str,
        decision_type: str,
        suggested_action: str,
        user_action: str,
        summary: str,
        portfolio_id: str | None = None,
        asset_id: str | None = None,
        source_recommendation_id: str | None = None,
        source_alert_id: str | None = None,
        workflow_run_id: str | None = None,
        reason_ids: list[str] | None = None,
        risk_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        created_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> DecisionLogORM:
        """记录系统建议、用户动作和证据引用。"""

        return self.decisions.insert_decision_log(
            decision_id=decision_id,
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            decision_type=decision_type,
            source_recommendation_id=source_recommendation_id,
            source_alert_id=source_alert_id,
            workflow_run_id=workflow_run_id,
            suggested_action=suggested_action,
            user_action=user_action,
            summary=summary,
            reason_ids=reason_ids,
            risk_ids=risk_ids,
            evidence_ids=evidence_ids,
            created_at=created_at,
            payload=payload,
        )

    def upsert_memory(
        self,
        *,
        memory_id: str,
        owner_id: str,
        memory_type: str,
        scope: str,
        content: str,
        confidence: Decimal = Decimal("1"),
        asset_id: str | None = None,
        source_decision_id: str | None = None,
        source_review_task_id: str | None = None,
        embedding_ref: str | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> AssistantMemoryORM:
        """新增或更新 Finance Memory。"""

        return self.memories.upsert_memory(
            memory_id=memory_id,
            owner_id=owner_id,
            memory_type=memory_type,
            scope=scope,
            asset_id=asset_id,
            source_decision_id=source_decision_id,
            source_review_task_id=source_review_task_id,
            content=content,
            embedding_ref=embedding_ref,
            confidence=confidence,
            status=status,
            payload=payload,
        )

    def link_memory_edge(
        self,
        *,
        edge_id: str,
        owner_id: str,
        source_type: str,
        source_id: str,
        relation_type: str,
        target_type: str,
        target_id: str,
        confidence: Decimal = Decimal("1"),
        reason: str | None = None,
        payload: JsonDict | None = None,
    ) -> FinancialMemoryEdgeORM:
        """建立 Finance Memory 轻量图谱关系。"""

        return self.memories.upsert_edge(
            edge_id=edge_id,
            owner_id=owner_id,
            source_type=source_type,
            source_id=source_id,
            relation_type=relation_type,
            target_type=target_type,
            target_id=target_id,
            confidence=confidence,
            reason=reason,
            payload=payload,
        )

    def schedule_review(
        self,
        *,
        review_task_id: str,
        owner_id: str,
        review_type: str,
        due_at: datetime,
        asset_id: str | None = None,
        source_decision_id: str | None = None,
        review_questions: list[JsonDict] | None = None,
        status: str = "pending",
        payload: JsonDict | None = None,
    ) -> ReviewTaskORM:
        """创建或更新复盘任务。"""

        return self.memories.upsert_review_task(
            review_task_id=review_task_id,
            owner_id=owner_id,
            asset_id=asset_id,
            source_decision_id=source_decision_id,
            review_type=review_type,
            due_at=due_at,
            status=status,
            review_questions=review_questions,
            payload=payload,
        )
