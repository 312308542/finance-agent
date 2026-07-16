"""从 PostgreSQL 事实库同步可重建知识图谱投影。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from finance_agent.graph.models import (
    GraphMutation,
    GraphNode,
    GraphRelationship,
    GraphSyncResult,
    graph_node_id,
    graph_relationship_id,
    json_safe,
)
from finance_agent.graph.stores import MemoryGraphStore, create_graph_store
from finance_agent.storage.event_validation import active_evidence_predicate
from finance_agent.storage.orm import (
    AssetORM,
    AssistantMemoryORM,
    DecisionLogORM,
    EvidenceORM,
    ReviewTaskORM,
    RiskFindingORM,
    WatchlistItemEventORM,
)
from finance_agent.storage.repositories import (
    AssetRepository,
    DecisionLogRepository,
    MemoryRepository,
)

JsonDict = dict[str, Any]


class GraphSyncService:
    """把金融事实和 Finance Memory 投影到配置选择的图数据库。"""

    def __init__(
        self,
        *,
        session: Session,
        graph_store: MemoryGraphStore | None = None,
    ) -> None:
        self.session = session
        self.graph_store = graph_store or create_graph_store()

    def sync_asset_graph(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 20,
    ) -> GraphSyncResult:
        """同步单标的决策、记忆、观察池事件、风险、证据和复盘关系。"""

        asset = AssetRepository(self.session).get_asset_or_none(asset_id)
        if asset is None:
            return self.graph_store.apply_mutation(
                GraphMutation(owner_id=owner_id, asset_id=asset_id, nodes=(), relationships=())
            )

        decisions = DecisionLogRepository(self.session).list_recent_decisions(
            owner_id=owner_id,
            asset_id=asset_id,
            limit=limit,
        )
        memories = MemoryRepository(self.session).list_active_memories(
            owner_id=owner_id,
            asset_id=asset_id,
            limit=limit,
        )
        watchlist_events = self._list_watchlist_events(
            owner_id=owner_id,
            asset_id=asset_id,
            limit=limit,
        )
        risks = self._list_risks(decisions=decisions, asset_id=asset_id, limit=limit)
        evidence = self._list_evidence(decisions=decisions, risks=risks, asset_id=asset_id)
        reviews = self._list_reviews(owner_id=owner_id, asset_id=asset_id, decisions=decisions)

        node_map: dict[str, GraphNode] = {}
        relationship_map: dict[str, GraphRelationship] = {}

        def add_node(node: GraphNode) -> None:
            node_map[node.node_id] = node

        def add_relationship(relationship: GraphRelationship) -> None:
            relationship_map[relationship.relationship_id] = relationship

        asset_node = build_asset_node(asset)
        add_node(asset_node)

        for decision in decisions:
            decision_node = build_decision_node(decision)
            add_node(decision_node)
            add_relationship(
                build_relationship(
                    relation_type="ABOUT",
                    source=decision_node,
                    target=asset_node,
                    owner_id=owner_id,
                    properties={"created_at": decision.created_at},
                )
            )

        for memory in memories:
            memory_node = build_memory_node(memory)
            add_node(memory_node)
            add_relationship(
                build_relationship(
                    relation_type="ABOUT",
                    source=memory_node,
                    target=asset_node,
                    owner_id=owner_id,
                    properties={"updated_at": memory.updated_at},
                )
            )
            if memory.source_decision_id:
                decision_node = node_map.get(graph_node_id("Decision", memory.source_decision_id))
                if decision_node:
                    add_relationship(
                        build_relationship(
                            relation_type="GENERATES",
                            source=decision_node,
                            target=memory_node,
                            owner_id=owner_id,
                            properties={"confidence": memory.confidence},
                        )
                    )
            if memory.source_review_task_id:
                review_node = node_map.get(graph_node_id("Review", memory.source_review_task_id))
                if review_node:
                    add_relationship(
                        build_relationship(
                            relation_type="GENERATES",
                            source=review_node,
                            target=memory_node,
                            owner_id=owner_id,
                            properties={"confidence": memory.confidence},
                        )
                    )

        memory_nodes_by_id = {
            node.properties.get("memory_id"): node
            for node in node_map.values()
            if "Memory" in node.labels
        }
        decision_nodes_by_id = {
            node.properties.get("decision_id"): node
            for node in node_map.values()
            if "Decision" in node.labels
        }

        for event in watchlist_events:
            event_node = build_watchlist_event_node(event)
            add_node(event_node)
            add_relationship(
                build_relationship(
                    relation_type="ABOUT",
                    source=event_node,
                    target=asset_node,
                    owner_id=owner_id,
                    properties={"created_at": event.created_at},
                )
            )
            memory_id = (event.payload or {}).get("memory_id")
            memory_node = memory_nodes_by_id.get(memory_id)
            if memory_node:
                add_relationship(
                    build_relationship(
                        relation_type="SUMMARIZED_BY",
                        source=event_node,
                        target=memory_node,
                        owner_id=owner_id,
                        properties={"event_type": event.event_type},
                    )
                )
            if event.source_decision_id:
                decision_node = decision_nodes_by_id.get(event.source_decision_id)
                if decision_node:
                    add_relationship(
                        build_relationship(
                            relation_type="TRIGGERED_BY",
                            source=event_node,
                            target=decision_node,
                            owner_id=owner_id,
                            properties={"event_type": event.event_type},
                        )
                    )

        for risk in risks:
            risk_node = build_risk_node(risk)
            add_node(risk_node)
            add_relationship(
                build_relationship(
                    relation_type="ABOUT",
                    source=risk_node,
                    target=asset_node,
                    owner_id=owner_id,
                    properties={"severity": risk.severity, "as_of": risk.as_of},
                )
            )
            for decision in decisions:
                if risk.risk_id in (decision.risk_ids or []):
                    decision_node = decision_nodes_by_id.get(decision.decision_id)
                    if decision_node:
                        add_relationship(
                            build_relationship(
                                relation_type="WARNED_BY",
                                source=decision_node,
                                target=risk_node,
                                owner_id=owner_id,
                                properties={"severity": risk.severity},
                            )
                        )

        evidence_nodes_by_id: dict[str, GraphNode] = {}
        for item in evidence:
            evidence_node = build_evidence_node(item)
            evidence_nodes_by_id[item.evidence_id] = evidence_node
            add_node(evidence_node)
            add_relationship(
                build_relationship(
                    relation_type="ABOUT",
                    source=evidence_node,
                    target=asset_node,
                    owner_id=owner_id,
                    properties={"source": item.source},
                )
            )

        for decision in decisions:
            decision_node = decision_nodes_by_id.get(decision.decision_id)
            if not decision_node:
                continue
            for evidence_id in decision.evidence_ids or []:
                evidence_node = evidence_nodes_by_id.get(evidence_id)
                if evidence_node:
                    add_relationship(
                        build_relationship(
                            relation_type="USES_EVIDENCE",
                            source=decision_node,
                            target=evidence_node,
                            owner_id=owner_id,
                            properties={"decision_type": decision.decision_type},
                        )
                    )

        for risk in risks:
            risk_node = node_map.get(graph_node_id("Risk", risk.risk_id))
            if not risk_node:
                continue
            for evidence_id in risk.evidence_ids or []:
                evidence_node = evidence_nodes_by_id.get(evidence_id)
                if evidence_node:
                    add_relationship(
                        build_relationship(
                            relation_type="USES_EVIDENCE",
                            source=risk_node,
                            target=evidence_node,
                            owner_id=owner_id,
                            properties={"risk_type": risk.risk_type},
                        )
                    )

        for review in reviews:
            review_node = build_review_node(review)
            add_node(review_node)
            add_relationship(
                build_relationship(
                    relation_type="ABOUT",
                    source=review_node,
                    target=asset_node,
                    owner_id=owner_id,
                    properties={"status": review.status},
                )
            )
            if review.source_decision_id:
                decision_node = decision_nodes_by_id.get(review.source_decision_id)
                if decision_node:
                    add_relationship(
                        build_relationship(
                            relation_type="REVIEWS",
                            source=review_node,
                            target=decision_node,
                            owner_id=owner_id,
                            properties={"review_type": review.review_type},
                        )
                    )

        mutation = GraphMutation(
            owner_id=owner_id,
            asset_id=asset_id,
            nodes=tuple(node_map.values()),
            relationships=tuple(relationship_map.values()),
            generated_at=datetime.now().astimezone(),
        )
        return self.graph_store.apply_mutation(mutation)

    def sync_owner_graph(
        self,
        *,
        owner_id: str,
        asset_ids: list[str] | None = None,
        limit_assets: int = 100,
        limit_per_asset: int = 20,
    ) -> JsonDict:
        """同步某个用户相关资产的图谱投影。"""

        resolved_asset_ids = asset_ids or self._list_owner_asset_ids(
            owner_id=owner_id,
            limit=limit_assets,
        )
        results = [
            self.sync_asset_graph(
                owner_id=owner_id,
                asset_id=asset_id,
                limit=limit_per_asset,
            ).to_dict()
            for asset_id in resolved_asset_ids[:limit_assets]
        ]
        return summarize_sync_results(
            graph_backend=self.graph_store.settings.backend,
            owner_id=owner_id,
            asset_ids=resolved_asset_ids[:limit_assets],
            results=results,
        )

    def sync_all_graph(
        self,
        *,
        owner_id: str | None = None,
        limit_assets: int = 200,
        limit_per_asset: int = 20,
    ) -> JsonDict:
        """同步全部或指定用户的图谱投影。"""

        if owner_id:
            return self.sync_owner_graph(
                owner_id=owner_id,
                limit_assets=limit_assets,
                limit_per_asset=limit_per_asset,
            )
        owner_ids = self._list_owner_ids(limit=limit_assets)
        owner_results = [
            self.sync_owner_graph(
                owner_id=resolved_owner_id,
                limit_assets=limit_assets,
                limit_per_asset=limit_per_asset,
            )
            for resolved_owner_id in owner_ids
        ]
        return {
            "graph_backend": self.graph_store.settings.backend,
            "owner_count": len(owner_results),
            "asset_count": sum(int(result["asset_count"]) for result in owner_results),
            "node_count": sum(int(result["node_count"]) for result in owner_results),
            "relationship_count": sum(
                int(result["relationship_count"]) for result in owner_results
            ),
            "applied_count": sum(int(result["applied_count"]) for result in owner_results),
            "owners": owner_results,
        }

    def _list_owner_asset_ids(self, *, owner_id: str, limit: int) -> list[str]:
        """从决策、记忆、观察池事件和复盘任务中提取用户相关资产。"""

        asset_ids: list[str] = []
        statements: tuple[Select[tuple[str]], ...] = (
            select(DecisionLogORM.asset_id)
            .where(DecisionLogORM.owner_id == owner_id, DecisionLogORM.asset_id.is_not(None))
            .order_by(DecisionLogORM.created_at.desc())
            .limit(limit),
            select(WatchlistItemEventORM.asset_id)
            .where(WatchlistItemEventORM.owner_id == owner_id)
            .order_by(WatchlistItemEventORM.created_at.desc())
            .limit(limit),
            select(ReviewTaskORM.asset_id)
            .where(ReviewTaskORM.owner_id == owner_id, ReviewTaskORM.asset_id.is_not(None))
            .order_by(ReviewTaskORM.due_at.desc())
            .limit(limit),
        )
        for statement in statements:
            for asset_id in self.session.scalars(statement):
                if asset_id and asset_id not in asset_ids:
                    asset_ids.append(asset_id)
                if len(asset_ids) >= limit:
                    return asset_ids
        statement = (
            select(AssistantMemoryORM.asset_id)
            .where(
                AssistantMemoryORM.owner_id == owner_id,
                AssistantMemoryORM.asset_id.is_not(None),
            )
            .order_by(AssistantMemoryORM.updated_at.desc())
            .limit(limit)
        )
        for asset_id in self.session.scalars(statement):
            if asset_id and asset_id not in asset_ids:
                asset_ids.append(asset_id)
            if len(asset_ids) >= limit:
                break
        return asset_ids

    def _list_owner_ids(self, *, limit: int) -> list[str]:
        """从用户相关表中提取 owner_id。"""

        owner_ids: list[str] = []
        statements = (
            select(DecisionLogORM.owner_id)
            .order_by(DecisionLogORM.created_at.desc())
            .limit(limit),
            select(WatchlistItemEventORM.owner_id)
            .order_by(WatchlistItemEventORM.created_at.desc())
            .limit(limit),
            select(ReviewTaskORM.owner_id).order_by(ReviewTaskORM.due_at.desc()).limit(limit),
        )
        for statement in statements:
            for resolved_owner_id in self.session.scalars(statement):
                if resolved_owner_id and resolved_owner_id not in owner_ids:
                    owner_ids.append(resolved_owner_id)
                if len(owner_ids) >= limit:
                    return owner_ids
        return owner_ids

    def _list_watchlist_events(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int,
    ) -> list[WatchlistItemEventORM]:
        statement = (
            select(WatchlistItemEventORM)
            .where(
                WatchlistItemEventORM.owner_id == owner_id,
                WatchlistItemEventORM.asset_id == asset_id,
            )
            .order_by(WatchlistItemEventORM.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def _list_risks(
        self,
        *,
        decisions: list[DecisionLogORM],
        asset_id: str,
        limit: int,
    ) -> list[RiskFindingORM]:
        risk_ids = {risk_id for decision in decisions for risk_id in (decision.risk_ids or [])}
        statement = select(RiskFindingORM).where(RiskFindingORM.asset_id == asset_id)
        if risk_ids:
            statement = statement.where(
                (RiskFindingORM.risk_id.in_(risk_ids)) | (RiskFindingORM.asset_id == asset_id)
            )
        return list(
            self.session.scalars(statement.order_by(RiskFindingORM.as_of.desc()).limit(limit))
        )

    def _list_evidence(
        self,
        *,
        decisions: list[DecisionLogORM],
        risks: list[RiskFindingORM],
        asset_id: str,
    ) -> list[EvidenceORM]:
        evidence_ids = {
            evidence_id for decision in decisions for evidence_id in (decision.evidence_ids or [])
        }
        evidence_ids.update(
            evidence_id for risk in risks for evidence_id in (risk.evidence_ids or [])
        )
        statement = select(EvidenceORM).where(
            EvidenceORM.asset_id == asset_id,
            active_evidence_predicate(EvidenceORM),
        )
        if evidence_ids:
            statement = statement.where(
                (EvidenceORM.evidence_id.in_(evidence_ids)) | (EvidenceORM.asset_id == asset_id)
            )
        return list(self.session.scalars(statement.order_by(EvidenceORM.collected_at.desc())))

    def _list_reviews(
        self,
        *,
        owner_id: str,
        asset_id: str,
        decisions: list[DecisionLogORM],
    ) -> list[ReviewTaskORM]:
        decision_ids = {decision.decision_id for decision in decisions}
        statement = select(ReviewTaskORM).where(
            ReviewTaskORM.owner_id == owner_id,
            ReviewTaskORM.asset_id == asset_id,
        )
        if decision_ids:
            statement = statement.where(
                (ReviewTaskORM.source_decision_id.in_(decision_ids))
                | (ReviewTaskORM.asset_id == asset_id)
            )
        return list(self.session.scalars(statement.order_by(ReviewTaskORM.due_at.desc())))


def build_asset_node(asset: AssetORM) -> GraphNode:
    """构建资产节点。"""

    return GraphNode(
        node_id=graph_node_id("Asset", asset.asset_id),
        labels=("Asset",),
        properties={
            "asset_id": asset.asset_id,
            "symbol": asset.symbol,
            "name": asset.name,
            "market": asset.market,
            "asset_type": asset.asset_type,
            "exchange": asset.exchange,
            "sector": asset.sector,
            "status": asset.status,
            "updated_at": asset.updated_at,
        },
    )


def build_decision_node(decision: DecisionLogORM) -> GraphNode:
    """构建决策节点。"""

    return GraphNode(
        node_id=graph_node_id("Decision", decision.decision_id),
        labels=("Decision",),
        properties={
            "decision_id": decision.decision_id,
            "owner_id": decision.owner_id,
            "asset_id": decision.asset_id,
            "decision_type": decision.decision_type,
            "suggested_action": decision.suggested_action,
            "user_action": decision.user_action,
            "summary": decision.summary,
            "reason_ids": decision.reason_ids or [],
            "risk_ids": decision.risk_ids or [],
            "evidence_ids": decision.evidence_ids or [],
            "created_at": decision.created_at,
        },
    )


def build_memory_node(memory: Any) -> GraphNode:
    """构建 Finance Memory 节点。"""

    return GraphNode(
        node_id=graph_node_id("Memory", memory.memory_id),
        labels=("Memory",),
        properties={
            "memory_id": memory.memory_id,
            "owner_id": memory.owner_id,
            "memory_type": memory.memory_type,
            "scope": memory.scope,
            "asset_id": memory.asset_id,
            "source_decision_id": memory.source_decision_id,
            "source_review_task_id": memory.source_review_task_id,
            "content": memory.content,
            "confidence": memory.confidence,
            "status": memory.status,
            "updated_at": memory.updated_at,
        },
    )


def build_watchlist_event_node(event: WatchlistItemEventORM) -> GraphNode:
    """构建观察池事件节点。"""

    return GraphNode(
        node_id=graph_node_id("WatchlistEvent", event.event_id),
        labels=("WatchlistEvent",),
        properties={
            "event_id": event.event_id,
            "owner_id": event.owner_id,
            "watchlist_id": event.watchlist_id,
            "watchlist_item_id": event.watchlist_item_id,
            "asset_id": event.asset_id,
            "event_type": event.event_type,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "reason": event.reason,
            "source_decision_id": event.source_decision_id,
            "created_at": event.created_at,
        },
    )


def build_risk_node(risk: RiskFindingORM) -> GraphNode:
    """构建风险节点。"""

    return GraphNode(
        node_id=graph_node_id("Risk", risk.risk_id),
        labels=("Risk",),
        properties={
            "risk_id": risk.risk_id,
            "asset_id": risk.asset_id,
            "scope": risk.scope,
            "risk_type": risk.risk_type,
            "severity": risk.severity,
            "score": risk.score,
            "title": risk.title,
            "description": risk.description,
            "evidence_ids": risk.evidence_ids or [],
            "as_of": risk.as_of,
        },
    )


def build_evidence_node(evidence: EvidenceORM) -> GraphNode:
    """构建证据节点。"""

    return GraphNode(
        node_id=graph_node_id("Evidence", evidence.evidence_id),
        labels=("Evidence",),
        properties={
            "evidence_id": evidence.evidence_id,
            "evidence_type": evidence.evidence_type,
            "asset_id": evidence.asset_id,
            "source": evidence.source,
            "title": evidence.title,
            "summary": evidence.summary,
            "data_ref": evidence.data_ref,
            "url": evidence.url,
            "reliability": evidence.reliability,
            "as_of": evidence.as_of,
            "collected_at": evidence.collected_at,
        },
    )


def build_review_node(review: ReviewTaskORM) -> GraphNode:
    """构建复盘节点。"""

    return GraphNode(
        node_id=graph_node_id("Review", review.review_task_id),
        labels=("Review",),
        properties={
            "review_task_id": review.review_task_id,
            "owner_id": review.owner_id,
            "asset_id": review.asset_id,
            "source_decision_id": review.source_decision_id,
            "review_type": review.review_type,
            "status": review.status,
            "due_at": review.due_at,
            "result_summary": review.result_summary,
            "finished_at": review.finished_at,
        },
    )


def build_relationship(
    *,
    relation_type: str,
    source: GraphNode,
    target: GraphNode,
    owner_id: str,
    properties: JsonDict | None = None,
) -> GraphRelationship:
    """构建稳定关系。"""

    merged_properties = {
        "owner_id": owner_id,
        **json_safe(properties or {}),
    }
    return GraphRelationship(
        relationship_id=graph_relationship_id(relation_type, source.node_id, target.node_id),
        type=relation_type,
        source_node_id=source.node_id,
        target_node_id=target.node_id,
        properties=merged_properties,
    )


def summarize_sync_results(
    *,
    graph_backend: str,
    owner_id: str,
    asset_ids: list[str],
    results: list[JsonDict],
) -> JsonDict:
    """汇总多资产图谱同步结果。"""

    return {
        "graph_backend": graph_backend,
        "owner_id": owner_id,
        "asset_count": len(asset_ids),
        "asset_ids": asset_ids,
        "node_count": sum(int(result["node_count"]) for result in results),
        "relationship_count": sum(int(result["relationship_count"]) for result in results),
        "applied_count": sum(1 for result in results if result.get("applied")),
        "results": results,
    }
