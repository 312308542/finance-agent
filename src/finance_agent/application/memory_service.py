"""Finance Memory 和决策日志应用服务。"""

from __future__ import annotations

from hashlib import sha1
import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    AssistantMemoryORM,
    DecisionLogORM,
    FinancialMemoryEdgeORM,
    MemoryEmbeddingORM,
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
            if memory is None or memory.status != "active":
                continue
            if memory.memory_id in seen_memory_ids:
                continue
            if not embedding.embedding:
                continue
            score = cosine_similarity(query_embedding, embedding.embedding)
            scored.append(
                {
                    "memory_id": memory.memory_id,
                    "score": round(score, 6),
                    "content": memory.content,
                    "memory_type": memory.memory_type,
                    "scope": memory.scope,
                    "asset_id": memory.asset_id,
                    "confidence": str(memory.confidence),
                    "embedding_id": embedding.embedding_id,
                    "embedding_model": embedding.embedding_model,
                    "updated_at": memory.updated_at.isoformat()
                    if memory.updated_at
                    else None,
                }
            )
            seen_memory_ids.add(memory.memory_id)

        if len(scored) < limit:
            indexed_ids = set(seen_memory_ids)
            for memory in self.memories.list_active_memories(
                owner_id=owner_id,
                asset_id=asset_id,
                memory_type=memory_type,
                limit=max(limit * 5, 20),
            ):
                if memory.memory_id in indexed_ids:
                    continue
                score = cosine_similarity(
                    query_embedding,
                    build_local_embedding(build_memory_embedding_text(memory)),
                )
                scored.append(
                    {
                        "memory_id": memory.memory_id,
                        "score": round(score, 6),
                        "content": memory.content,
                        "memory_type": memory.memory_type,
                        "scope": memory.scope,
                        "asset_id": memory.asset_id,
                        "confidence": str(memory.confidence),
                        "embedding_id": None,
                        "embedding_model": embedding_model,
                        "updated_at": memory.updated_at.isoformat()
                        if memory.updated_at
                        else None,
                    }
                )

        return sorted(scored, key=lambda item: item["score"], reverse=True)[:limit]

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
        self.index_memory_embedding(memory_id=memory_id)
        return completed


def build_memory_embedding_id(memory_id: str) -> str:
    """生成 Finance Memory embedding ID。"""

    return f"emb:{memory_id}"


def build_feedback_memory_id(decision_id: str) -> str:
    """生成用户反馈对应的 Finance Memory ID。"""

    return f"memory:{decision_id}:feedback"


def build_review_result_memory_id(review_task_id: str) -> str:
    """生成复盘结果对应的 Finance Memory ID。"""

    return f"memory:{review_task_id}:review_result"


def build_memory_embedding_text(memory: AssistantMemoryORM) -> str:
    """构造轻量向量索引用文本。"""

    parts = [
        memory.memory_type,
        memory.scope,
        memory.asset_id or "",
        memory.content,
    ]
    return "\n".join(part for part in parts if part)


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
