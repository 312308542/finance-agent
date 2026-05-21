"""Finance Memory 和决策日志应用服务。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha1
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    AssistantMemoryORM,
    DecisionLogORM,
    FinancialMemoryEdgeORM,
    MemoryEmbeddingORM,
    MonitoringAlertORM,
    ReviewTaskORM,
    WatchlistItemEventORM,
)
from finance_agent.storage.repositories import (
    DecisionLogRepository,
    MemoryRepository,
    WatchlistRepository,
)

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class WatchlistDailyReasonRecord:
    """每日关注原因沉淀结果。"""

    event: WatchlistItemEventORM
    memory: AssistantMemoryORM


class MemoryService:
    """金融业务记忆服务。

    Finance Memory 只保存可审计的金融业务上下文，不保存 Hermes-agent 的通用对话记忆。
    """

    def __init__(self, session: Session) -> None:
        self.decisions = DecisionLogRepository(session)
        self.memories = MemoryRepository(session)
        self.watchlists = WatchlistRepository(session)

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
        auto_index: bool = True,
    ) -> AssistantMemoryORM:
        """新增或更新 Finance Memory。"""

        memory = self.memories.upsert_memory(
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
        if auto_index and status in {"active", "stale"}:
            self.index_memory_embedding(memory_id=memory.memory_id)
            refreshed = self.memories.get_memory(memory.memory_id)
            if refreshed is not None:
                return refreshed
        return memory

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

    def index_memory_embedding(
        self,
        *,
        memory_id: str,
        embedding_model: str = "local-hash-v1",
        dimensions: int = 32,
    ) -> MemoryEmbeddingORM:
        """为 Finance Memory 写入本地轻量向量索引。"""

        memory = self.memories.get_memory(memory_id)
        if memory is None:
            raise ValueError(f"找不到 Finance Memory：{memory_id}")

        embedding_id = build_memory_embedding_id(memory_id)
        chunk_text = build_memory_embedding_text(memory)
        embedding = self.memories.upsert_embedding(
            embedding_id=embedding_id,
            owner_id=memory.owner_id,
            memory_id=memory.memory_id,
            source_type="assistant_memory",
            source_id=memory.memory_id,
            chunk_text=chunk_text,
            embedding_model=embedding_model,
            embedding=build_local_embedding(chunk_text, dimensions=dimensions),
            payload={
                "memory_type": memory.memory_type,
                "scope": memory.scope,
                "asset_id": memory.asset_id,
            },
        )
        if memory.embedding_ref != embedding_id:
            self.memories.upsert_memory(
                memory_id=memory.memory_id,
                owner_id=memory.owner_id,
                memory_type=memory.memory_type,
                scope=memory.scope,
                asset_id=memory.asset_id,
                source_decision_id=memory.source_decision_id,
                source_review_task_id=memory.source_review_task_id,
                content=memory.content,
                embedding_ref=embedding_id,
                confidence=memory.confidence,
                status=memory.status,
                payload=memory.payload,
            )
        return embedding

    def recall_similar_memories(
        self,
        *,
        owner_id: str,
        query: str,
        asset_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
        embedding_model: str = "local-hash-v1",
        as_of: datetime | None = None,
        include_statuses: tuple[str, ...] = ("active",),
        half_life_days: int = 90,
    ) -> list[JsonDict]:
        """基于本地向量从 Finance Memory 中召回相似记忆。"""

        query_embedding = build_local_embedding(query)
        rows = self.memories.list_embeddings(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type=memory_type,
            limit=max(limit * 20, 50),
        )
        scored: list[JsonDict] = []
        seen_memory_ids: set[str] = set()
        for embedding, memory in rows:
            if memory is None or memory.status not in include_statuses:
                continue
            if memory.memory_id in seen_memory_ids:
                continue
            if not embedding.embedding:
                continue
            score = cosine_similarity(query_embedding, embedding.embedding)
            scored.append(
                build_memory_recall_item(
                    memory=memory,
                    score=score,
                    embedding_id=embedding.embedding_id,
                    embedding_model=embedding.embedding_model,
                    as_of=as_of,
                    half_life_days=half_life_days,
                )
            )
            seen_memory_ids.add(memory.memory_id)

        if len(scored) < limit:
            indexed_ids = set(seen_memory_ids)
            for memory in self.memories.list_memories(
                owner_id=owner_id,
                asset_id=asset_id,
                memory_type=memory_type,
                statuses=include_statuses,
                limit=max(limit * 5, 20),
            ):
                if memory.memory_id in indexed_ids:
                    continue
                score = cosine_similarity(
                    query_embedding,
                    build_local_embedding(build_memory_embedding_text(memory)),
                )
                scored.append(
                    build_memory_recall_item(
                        memory=memory,
                        score=score,
                        embedding_id=None,
                        embedding_model=embedding_model,
                        as_of=as_of,
                        half_life_days=half_life_days,
                    )
                )

        return sorted(scored, key=lambda item: item["ranking_score"], reverse=True)[:limit]

    def get_asset_memory_timeline(
        self,
        *,
        owner_id: str,
        asset_id: str,
        memory_type: str | None = None,
        limit: int = 20,
        include_statuses: tuple[str, ...] = ("active", "stale"),
    ) -> list[JsonDict]:
        """按时间倒序读取单标的 Finance Memory 时间线。"""

        memories = self.memories.list_memories(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type=memory_type,
            statuses=include_statuses,
            limit=limit,
        )
        return [serialize_memory_timeline_item(memory) for memory in memories]

    def build_memory_context(
        self,
        *,
        owner_id: str,
        asset_id: str,
        query: str,
        memory_type: str | None = None,
        limit: int = 10,
        as_of: datetime | None = None,
    ) -> JsonDict:
        """为 Agent 构造按需读取的 Finance Memory 上下文。"""

        similar_memories = self.recall_similar_memories(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type=memory_type,
            query=query,
            limit=limit,
            as_of=as_of,
        )
        timeline = self.get_asset_memory_timeline(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type=memory_type,
            limit=limit,
        )
        return {
            "owner_id": owner_id,
            "asset_id": asset_id,
            "query": query,
            "similar_memories": similar_memories,
            "timeline": timeline,
        }

    def record_watchlist_daily_reason(
        self,
        *,
        owner_id: str,
        watchlist_id: str,
        watchlist_item_id: str,
        asset_id: str,
        symbol: str,
        daily_watch_reason: str,
        as_of: datetime,
        next_status: str,
        original_intake_reason: str | None = None,
        risk_rebuttal: str | None = None,
        source_decision_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> WatchlistDailyReasonRecord:
        """记录并沉淀候选池/观察池每日继续关注原因。"""

        event_payload = {
            **(payload or {}),
            "daily_watch_reason": daily_watch_reason,
            "original_intake_reason": original_intake_reason,
            "next_status": next_status,
            "risk_rebuttal": risk_rebuttal,
        }
        event = self.watchlists.insert_watchlist_event(
            event_id=build_watchlist_daily_reason_event_id(
                watchlist_item_id=watchlist_item_id,
                as_of=as_of,
            ),
            owner_id=owner_id,
            watchlist_id=watchlist_id,
            watchlist_item_id=watchlist_item_id,
            asset_id=asset_id,
            event_type="daily_watch_reason",
            from_status=None,
            to_status=next_status,
            reason=daily_watch_reason,
            source_decision_id=source_decision_id,
            created_at=as_of,
            payload=event_payload,
        )
        memory = self.upsert_memory(
            memory_id=build_watchlist_daily_reason_memory_id(
                owner_id=owner_id,
                watchlist_item_id=watchlist_item_id,
                as_of=as_of,
            ),
            owner_id=owner_id,
            memory_type="watchlist_daily_reason",
            scope="asset",
            asset_id=asset_id,
            source_decision_id=source_decision_id,
            content=build_watchlist_daily_reason_content(
                symbol=symbol,
                daily_watch_reason=daily_watch_reason,
                risk_rebuttal=risk_rebuttal,
            ),
            confidence=Decimal("0.780000"),
            payload={
                **event_payload,
                "watchlist_id": watchlist_id,
                "watchlist_item_id": watchlist_item_id,
                "source_type": "watchlist_item_event",
                "source_id": event.event_id,
            },
        )
        self.link_memory_edge(
            edge_id=build_memory_edge_id(
                owner_id=owner_id,
                source_type="watchlist_event",
                source_id=event.event_id,
                relation_type="summarizes",
                target_type="memory",
                target_id=memory.memory_id,
            ),
            owner_id=owner_id,
            source_type="watchlist_event",
            source_id=event.event_id,
            relation_type="summarizes",
            target_type="memory",
            target_id=memory.memory_id,
            confidence=Decimal("0.900000"),
            reason="每日继续关注原因沉淀为 Finance Memory。",
        )
        return WatchlistDailyReasonRecord(event=event, memory=memory)

    def record_user_feedback(
        self,
        *,
        feedback_id: str,
        owner_id: str,
        feedback_type: str,
        suggested_action: str,
        user_action: str,
        summary: str,
        as_of: datetime,
        asset_id: str | None = None,
        portfolio_id: str | None = None,
        source_memory_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> DecisionLogORM:
        """记录用户反馈，并反写为可召回的 Finance Memory。"""

        feedback_payload = dict(payload or {})
        if source_memory_id:
            feedback_payload["source_memory_id"] = source_memory_id
        decision = self.record_decision(
            decision_id=feedback_id,
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            decision_type=feedback_type,
            suggested_action=suggested_action,
            user_action=user_action,
            summary=summary,
            created_at=as_of,
            payload=feedback_payload,
        )
        memory_id = build_feedback_memory_id(decision.decision_id)
        self.upsert_memory(
            memory_id=memory_id,
            owner_id=owner_id,
            memory_type="user_feedback",
            scope="asset" if asset_id else "owner",
            asset_id=asset_id,
            source_decision_id=decision.decision_id,
            content=summary,
            confidence=Decimal("1"),
            payload={
                **feedback_payload,
                "feedback_type": feedback_type,
                "suggested_action": suggested_action,
                "user_action": user_action,
            },
        )
        self.link_memory_edge(
            edge_id=build_memory_edge_id(
                owner_id=owner_id,
                source_type="decision",
                source_id=decision.decision_id,
                relation_type="supports",
                target_type="memory",
                target_id=memory_id,
            ),
            owner_id=owner_id,
            source_type="decision",
            source_id=decision.decision_id,
            relation_type="supports",
            target_type="memory",
            target_id=memory_id,
            reason="用户反馈反写为长期业务记忆。",
        )
        if source_memory_id:
            self.link_memory_edge(
                edge_id=build_memory_edge_id(
                    owner_id=owner_id,
                    source_type="memory",
                    source_id=memory_id,
                    relation_type="responds_to",
                    target_type="memory",
                    target_id=source_memory_id,
                ),
                owner_id=owner_id,
                source_type="memory",
                source_id=memory_id,
                relation_type="responds_to",
                target_type="memory",
                target_id=source_memory_id,
                reason="用户反馈回应了既有记忆。",
            )
            self.apply_feedback_to_source_memory(
                source_memory_id=source_memory_id,
                feedback_decision_id=decision.decision_id,
                feedback_memory_id=memory_id,
                user_action=user_action,
                feedback_type=feedback_type,
                summary=summary,
                as_of=as_of,
            )
        self.index_memory_embedding(memory_id=memory_id)
        return decision

    def complete_review_task(
        self,
        *,
        review_task_id: str,
        owner_id: str,
        result_summary: str,
        finished_at: datetime,
        outcome: str | None = None,
        payload: JsonDict | None = None,
    ) -> ReviewTaskORM:
        """完成复盘任务，并把复盘结论反写到 Finance Memory。"""

        task = self.memories.get_review_task(review_task_id)
        if task is None:
            raise ValueError(f"找不到复盘任务：{review_task_id}")
        if task.owner_id != owner_id:
            raise ValueError(f"复盘任务不属于当前 owner：{review_task_id}")

        merged_payload = dict(task.payload or {})
        merged_payload.update(payload or {})
        if outcome:
            merged_payload["outcome"] = outcome
        completed = self.memories.upsert_review_task(
            review_task_id=task.review_task_id,
            owner_id=task.owner_id,
            asset_id=task.asset_id,
            source_decision_id=task.source_decision_id,
            review_type=task.review_type,
            due_at=task.due_at,
            status="completed",
            review_questions=task.review_questions,
            result_summary=result_summary,
            finished_at=finished_at,
            payload=merged_payload,
        )

        memory_id = build_review_result_memory_id(review_task_id)
        self.upsert_memory(
            memory_id=memory_id,
            owner_id=owner_id,
            memory_type="review_result",
            scope="asset" if completed.asset_id else "owner",
            asset_id=completed.asset_id,
            source_review_task_id=completed.review_task_id,
            content=result_summary,
            confidence=Decimal("0.950000"),
            payload={
                **merged_payload,
                "review_type": completed.review_type,
                "source_decision_id": completed.source_decision_id,
            },
        )
        self.link_memory_edge(
            edge_id=build_memory_edge_id(
                owner_id=owner_id,
                source_type="review_task",
                source_id=completed.review_task_id,
                relation_type="summarizes",
                target_type="memory",
                target_id=memory_id,
            ),
            owner_id=owner_id,
            source_type="review_task",
            source_id=completed.review_task_id,
            relation_type="summarizes",
            target_type="memory",
            target_id=memory_id,
            reason="复盘任务完成后沉淀为长期业务记忆。",
        )
        if completed.source_decision_id:
            self.link_memory_edge(
                edge_id=build_memory_edge_id(
                    owner_id=owner_id,
                    source_type="memory",
                    source_id=memory_id,
                    relation_type="reviews",
                    target_type="decision",
                    target_id=completed.source_decision_id,
                ),
                owner_id=owner_id,
                source_type="memory",
                source_id=memory_id,
                relation_type="reviews",
                target_type="decision",
                target_id=completed.source_decision_id,
                reason="复盘结论关联原始决策。",
            )
            self.apply_review_result_to_source_memories(
                owner_id=owner_id,
                source_decision_id=completed.source_decision_id,
                review_task_id=completed.review_task_id,
                review_memory_id=memory_id,
                result_summary=result_summary,
                outcome=outcome,
                finished_at=finished_at,
                asset_id=completed.asset_id,
            )
        self.index_memory_embedding(memory_id=memory_id)
        return completed

    def apply_feedback_to_source_memory(
        self,
        *,
        source_memory_id: str,
        feedback_decision_id: str,
        feedback_memory_id: str,
        user_action: str,
        feedback_type: str,
        summary: str,
        as_of: datetime,
    ) -> AssistantMemoryORM | None:
        """把用户反馈回写到源记忆，用于后续召回降权或增权。"""

        source = self.memories.get_memory(source_memory_id)
        if source is None:
            return None
        updated_confidence = adjust_confidence_by_feedback(
            source.confidence,
            user_action=user_action,
        )
        feedback_item = {
            "feedback_decision_id": feedback_decision_id,
            "feedback_memory_id": feedback_memory_id,
            "feedback_type": feedback_type,
            "user_action": user_action,
            "summary": summary,
            "as_of": as_of.isoformat(),
        }
        payload = append_payload_history(
            source.payload or {},
            key="feedback_history",
            item=feedback_item,
            max_items=20,
        )
        payload.update(
            {
                "last_feedback_decision_id": feedback_decision_id,
                "last_feedback_action": user_action,
                "last_feedback_at": as_of.isoformat(),
            }
        )
        return self.upsert_memory(
            memory_id=source.memory_id,
            owner_id=source.owner_id,
            memory_type=source.memory_type,
            scope=source.scope,
            asset_id=source.asset_id,
            source_decision_id=source.source_decision_id,
            source_review_task_id=source.source_review_task_id,
            content=source.content,
            embedding_ref=source.embedding_ref,
            confidence=updated_confidence,
            status=source.status,
            payload=payload,
        )

    def apply_review_result_to_source_memories(
        self,
        *,
        owner_id: str,
        source_decision_id: str,
        review_task_id: str,
        review_memory_id: str,
        result_summary: str,
        outcome: str | None,
        finished_at: datetime,
        asset_id: str | None = None,
    ) -> list[AssistantMemoryORM]:
        """把复盘结论回写到同源决策记忆。"""

        source_memories = self.memories.list_memories_by_source_decision(
            owner_id=owner_id,
            source_decision_id=source_decision_id,
            asset_id=asset_id,
            statuses=("active", "stale"),
            limit=20,
        )
        updated: list[AssistantMemoryORM] = []
        for source in source_memories:
            if source.memory_id == review_memory_id:
                continue
            payload = append_payload_history(
                source.payload or {},
                key="review_history",
                item={
                    "review_task_id": review_task_id,
                    "review_memory_id": review_memory_id,
                    "outcome": outcome,
                    "summary": result_summary,
                    "finished_at": finished_at.isoformat(),
                },
                max_items=20,
            )
            payload.update(
                {
                    "last_review_task_id": review_task_id,
                    "last_review_outcome": outcome,
                    "last_review_at": finished_at.isoformat(),
                }
            )
            updated_confidence = adjust_confidence_by_review(
                source.confidence,
                outcome=outcome,
            )
            updated.append(
                self.upsert_memory(
                    memory_id=source.memory_id,
                    owner_id=source.owner_id,
                    memory_type=source.memory_type,
                    scope=source.scope,
                    asset_id=source.asset_id,
                    source_decision_id=source.source_decision_id,
                    source_review_task_id=source.source_review_task_id,
                    content=source.content,
                    embedding_ref=source.embedding_ref,
                    confidence=updated_confidence,
                    status=source.status,
                    payload=payload,
                )
            )
            self.link_memory_edge(
                edge_id=build_memory_edge_id(
                    owner_id=owner_id,
                    source_type="memory",
                    source_id=review_memory_id,
                    relation_type="updates_confidence_of",
                    target_type="memory",
                    target_id=source.memory_id,
                ),
                owner_id=owner_id,
                source_type="memory",
                source_id=review_memory_id,
                relation_type="updates_confidence_of",
                target_type="memory",
                target_id=source.memory_id,
                confidence=Decimal("0.850000"),
                reason="复盘结论修正了历史决策记忆可信度。",
            )
        return updated

    def decay_memory_confidence(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        memory_type: str | None = None,
        as_of: datetime | None = None,
        half_life_days: int = 90,
        stale_threshold: Decimal = Decimal("0.250000"),
        archive_threshold: Decimal = Decimal("0.080000"),
        limit: int = 500,
    ) -> JsonDict:
        """按时间半衰期降低旧记忆可信度，并标记 stale / archived。"""

        evaluation_time = as_of or datetime.now().astimezone()
        memories = self.memories.list_memories(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type=memory_type,
            statuses=("active", "stale"),
            limit=limit,
        )
        updated: list[JsonDict] = []
        for memory in memories:
            base_time = memory.updated_at or memory.created_at
            age_days = max((evaluation_time - base_time).total_seconds() / 86400, 0.0)
            recency_weight = calculate_recency_weight(
                updated_at=base_time,
                as_of=evaluation_time,
                half_life_days=half_life_days,
            )
            decayed_confidence = quantize_confidence(
                Decimal(str(float(memory.confidence) * recency_weight))
            )
            if decayed_confidence >= memory.confidence and memory.status == "active":
                continue
            status = memory.status
            if decayed_confidence <= archive_threshold:
                status = "archived"
            elif decayed_confidence <= stale_threshold:
                status = "stale"
            payload = dict(memory.payload or {})
            payload["decay"] = {
                "as_of": evaluation_time.isoformat(),
                "age_days": round(age_days, 4),
                "half_life_days": half_life_days,
                "recency_weight": round(recency_weight, 6),
                "previous_confidence": str(memory.confidence),
                "decayed_confidence": str(decayed_confidence),
                "status": status,
            }
            saved = self.upsert_memory(
                memory_id=memory.memory_id,
                owner_id=memory.owner_id,
                memory_type=memory.memory_type,
                scope=memory.scope,
                asset_id=memory.asset_id,
                source_decision_id=memory.source_decision_id,
                source_review_task_id=memory.source_review_task_id,
                content=memory.content,
                embedding_ref=memory.embedding_ref,
                confidence=decayed_confidence,
                status=status,
                payload=payload,
                auto_index=status != "archived",
            )
            updated.append(serialize_memory_timeline_item(saved))
        return {
            "owner_id": owner_id,
            "asset_id": asset_id,
            "memory_type": memory_type,
            "as_of": evaluation_time.isoformat(),
            "half_life_days": half_life_days,
            "updated_count": len(updated),
            "updated_memories": updated,
        }


def build_memory_embedding_id(memory_id: str) -> str:
    """生成 Finance Memory embedding ID。"""

    return f"emb:{memory_id}"


def build_feedback_memory_id(decision_id: str) -> str:
    """生成用户反馈对应的 Finance Memory ID。"""

    return f"memory:{decision_id}:feedback"


def build_review_result_memory_id(review_task_id: str) -> str:
    """生成复盘结果对应的 Finance Memory ID。"""

    return f"memory:{review_task_id}:review_result"


def build_watchlist_daily_reason_event_id(
    *,
    watchlist_item_id: str,
    as_of: datetime,
) -> str:
    """生成每日关注原因事件 ID。"""

    digest = sha1(f"{watchlist_item_id}:daily_watch_reason:{as_of:%Y%m%d}".encode()).hexdigest()
    return f"watchlist_event:daily_watch_reason:{as_of:%Y%m%d}:{digest[:24]}"


def build_watchlist_daily_reason_memory_id(
    *,
    owner_id: str,
    watchlist_item_id: str,
    as_of: datetime,
) -> str:
    """生成每日关注原因记忆 ID。"""

    digest = sha1(f"{owner_id}:{watchlist_item_id}:{as_of:%Y%m%d}".encode()).hexdigest()[:12]
    return f"memory:watchlist_daily_reason:{as_of:%Y%m%d}:{digest}"


def build_watchlist_daily_reason_content(
    *,
    symbol: str,
    daily_watch_reason: str,
    risk_rebuttal: str | None,
) -> str:
    """生成每日关注原因记忆正文。"""

    content = f"{symbol} 今日继续关注原因：{daily_watch_reason}"
    if risk_rebuttal:
        content = f"{content} 风险反驳：{risk_rebuttal}"
    return content


def build_memory_embedding_text(memory: AssistantMemoryORM) -> str:
    """构造轻量向量索引用文本。"""

    parts = [
        memory.memory_type,
        memory.scope,
        memory.asset_id or "",
        memory.content,
    ]
    return "\n".join(part for part in parts if part)


def serialize_memory_timeline_item(memory: AssistantMemoryORM) -> JsonDict:
    """序列化 Finance Memory 时间线条目。"""

    return {
        "memory_id": memory.memory_id,
        "owner_id": memory.owner_id,
        "memory_type": memory.memory_type,
        "scope": memory.scope,
        "asset_id": memory.asset_id,
        "source_decision_id": memory.source_decision_id,
        "source_review_task_id": memory.source_review_task_id,
        "content": memory.content,
        "embedding_ref": memory.embedding_ref,
        "confidence": str(memory.confidence),
        "status": memory.status,
        "created_at": memory.created_at.isoformat() if memory.created_at else None,
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
        "payload": memory.payload or {},
    }


def build_memory_recall_item(
    *,
    memory: AssistantMemoryORM,
    score: float,
    embedding_id: str | None,
    embedding_model: str,
    as_of: datetime | None,
    half_life_days: int,
) -> JsonDict:
    """构造带置信度和时间衰减的召回条目。"""

    evaluation_time = as_of or datetime.now().astimezone()
    recency_weight = calculate_recency_weight(
        updated_at=memory.updated_at or memory.created_at,
        as_of=evaluation_time,
        half_life_days=half_life_days,
    )
    effective_confidence = float(memory.confidence) * recency_weight
    ranking_score = max(score, 0.0) * max(effective_confidence, 0.0)
    return {
        "memory_id": memory.memory_id,
        "score": round(score, 6),
        "ranking_score": round(ranking_score, 6),
        "effective_confidence": round(effective_confidence, 6),
        "recency_weight": round(recency_weight, 6),
        "content": memory.content,
        "memory_type": memory.memory_type,
        "scope": memory.scope,
        "asset_id": memory.asset_id,
        "confidence": str(memory.confidence),
        "status": memory.status,
        "embedding_id": embedding_id,
        "embedding_model": embedding_model,
        "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
        "payload": memory.payload or {},
    }


def build_memory_edge_id(
    *,
    owner_id: str,
    source_type: str,
    source_id: str,
    relation_type: str,
    target_type: str,
    target_id: str,
) -> str:
    """生成可重复的记忆图谱边 ID。"""

    digest = sha1(
        "|".join(
            (owner_id, source_type, source_id, relation_type, target_type, target_id)
        ).encode()
    ).hexdigest()[:16]
    return f"memory_edge:{digest}"


def build_local_embedding(text: str, *, dimensions: int = 32) -> list[float]:
    """用本地 hash 特征生成轻量 embedding，便于无外部模型时召回。"""

    if dimensions <= 0:
        raise ValueError("embedding dimensions 必须大于 0。")
    vector = [0.0] * dimensions
    for term in iter_embedding_terms(text):
        digest = sha1(term.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        weight = 1.0 + min(len(term), 6) * 0.05
        vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [round(value / norm, 8) for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算两个向量的余弦相似度。"""

    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    dot = sum(left[index] * right[index] for index in range(length))
    left_norm = math.sqrt(sum(value * value for value in left[:length]))
    right_norm = math.sqrt(sum(value * value for value in right[:length]))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def calculate_recency_weight(
    *,
    updated_at: datetime | None,
    as_of: datetime,
    half_life_days: int,
) -> float:
    """按半衰期计算时间衰减权重。"""

    if updated_at is None:
        return 1.0
    if half_life_days <= 0:
        raise ValueError("half_life_days 必须大于 0。")
    age_days = max((as_of - updated_at).total_seconds() / 86400, 0.0)
    return math.pow(0.5, age_days / half_life_days)


def quantize_confidence(value: Decimal) -> Decimal:
    """把置信度裁剪到 0-1 并保留 6 位小数。"""

    clipped = max(Decimal("0"), min(Decimal("1"), value))
    return clipped.quantize(Decimal("0.000001"))


def adjust_confidence_by_feedback(confidence: Decimal, *, user_action: str) -> Decimal:
    """根据用户反馈动作调整源记忆置信度。"""

    action = user_action.strip().lower()
    if action in {"accepted", "accept", "confirmed", "confirm", "followed", "adopted"}:
        return quantize_confidence(confidence + Decimal("0.080000"))
    if action in {"rejected", "reject", "declined", "ignored", "cancelled", "canceled"}:
        return quantize_confidence(confidence - Decimal("0.150000"))
    if action in {"modified", "adjusted", "partial"}:
        return quantize_confidence(confidence - Decimal("0.060000"))
    return confidence


def adjust_confidence_by_review(confidence: Decimal, *, outcome: str | None) -> Decimal:
    """根据复盘结果调整源记忆置信度。"""

    normalized = (outcome or "").strip().lower()
    positive = {"confirmed", "works", "hit_target", "valid", "success"}
    negative = {"failed", "invalid", "stop_loss", "wrong", "reversed"}
    uncertain = {"needs_more_confirmation", "partial", "uncertain", "mixed"}
    if normalized in positive:
        return quantize_confidence(confidence + Decimal("0.060000"))
    if normalized in negative:
        return quantize_confidence(confidence - Decimal("0.180000"))
    if normalized in uncertain:
        return quantize_confidence(confidence - Decimal("0.100000"))
    return quantize_confidence(confidence - Decimal("0.040000"))


def append_payload_history(
    payload: JsonDict,
    *,
    key: str,
    item: JsonDict,
    max_items: int,
) -> JsonDict:
    """向 payload 追加审计历史，并限制长度。"""

    updated = dict(payload)
    history = list(updated.get(key) or [])
    history.append(item)
    updated[key] = history[-max_items:]
    return updated


def iter_embedding_terms(text: str) -> list[str]:
    """把中英文金融文本拆成适合本地召回的 hash 特征。"""

    normalized = (text or "").lower()
    terms: list[str] = []
    terms.extend(re.findall(r"[a-z0-9][a-z0-9_.:-]*", normalized))
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", normalized)
    terms.extend(cjk_chars)
    terms.extend(
        "".join(cjk_chars[index : index + 2])
        for index in range(max(len(cjk_chars) - 1, 0))
    )
    terms.extend(
        "".join(cjk_chars[index : index + 3])
        for index in range(max(len(cjk_chars) - 2, 0))
    )
    return [term for term in terms if term.strip()]
