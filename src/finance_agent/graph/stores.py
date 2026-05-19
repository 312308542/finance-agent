"""知识图谱存储后端。

正式运行时通过配置显式选择 Neo4j / DozerDB 或 Apache AGE。DryRun 后端只用于
冒烟、单元验证和本地无图数据库时的协议测试，不代表生产 fallback。
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from dataclasses import replace
from typing import Any, Protocol

from finance_agent.graph.models import (
    GraphMutation,
    GraphNode,
    GraphPath,
    GraphQueryResult,
    GraphRelationship,
    GraphSyncResult,
    compact_json,
    graph_node_id,
    json_safe,
)
from finance_agent.graph.settings import GraphStoreSettings, load_graph_store_settings

JsonDict = dict[str, Any]


class MemoryGraphStore(Protocol):
    """Finance Memory 知识图谱统一接口。"""

    settings: GraphStoreSettings

    def initialize_schema(self) -> JsonDict:
        """初始化图数据库约束、索引或图空间。"""

    def health_check(self) -> JsonDict:
        """检查图数据库后端连通性和可选图算法能力。"""

    def apply_mutation(self, mutation: GraphMutation) -> GraphSyncResult:
        """写入图谱投影变更。"""

    def trace_asset_graph(
        self,
        *,
        owner_id: str,
        asset_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> GraphQueryResult:
        """追踪单标的关联路径。"""

    def explain_candidate_reason_chain(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 5,
    ) -> GraphQueryResult:
        """解释标的入池和持续观察原因链。"""

    def find_memory_conflicts(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 10,
    ) -> GraphQueryResult:
        """发现同一标的记忆中的明显冲突。"""

    def find_similar_decision_paths(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 10,
    ) -> GraphQueryResult:
        """查找与目标标的结构相似的历史决策路径。"""

    def detect_risk_contagion(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        max_depth: int = 3,
        limit: int = 20,
    ) -> GraphQueryResult:
        """检测风险、证据、决策、记忆与资产之间的传导路径。"""


class DisabledGraphStore:
    """未启用图谱时的显式空后端。"""

    def __init__(self, settings: GraphStoreSettings) -> None:
        self.settings = settings

    def initialize_schema(self) -> JsonDict:
        """未启用时返回显式状态。"""

        return {
            "backend": self.settings.backend,
            "enabled": False,
            "initialized": False,
            "message": "GraphStore 未启用，未初始化图数据库。",
        }

    def health_check(self) -> JsonDict:
        """未启用时返回显式健康状态。"""

        return {
            "backend": self.settings.backend,
            "enabled": False,
            "healthy": False,
            "message": "GraphStore 未启用。",
        }

    def apply_mutation(self, mutation: GraphMutation) -> GraphSyncResult:
        """跳过写入，并返回禁用原因。"""

        return GraphSyncResult(
            graph_backend=self.settings.backend,
            node_count=len(mutation.nodes),
            relationship_count=len(mutation.relationships),
            applied=False,
            warnings=("GraphStore 未启用，未写入图数据库。",),
            metadata={"enabled": False},
        )

    def trace_asset_graph(
        self,
        *,
        owner_id: str,
        asset_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> GraphQueryResult:
        """返回空路径，并说明图谱未启用。"""

        return _disabled_query_result(self.settings.backend)

    def explain_candidate_reason_chain(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 5,
    ) -> GraphQueryResult:
        """返回空原因链，并说明图谱未启用。"""

        return _disabled_query_result(self.settings.backend)

    def find_memory_conflicts(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 10,
    ) -> GraphQueryResult:
        """返回空冲突，并说明图谱未启用。"""

        return _disabled_query_result(self.settings.backend)

    def find_similar_decision_paths(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 10,
    ) -> GraphQueryResult:
        """返回空相似决策，并说明图谱未启用。"""

        return _disabled_query_result(self.settings.backend)

    def detect_risk_contagion(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        max_depth: int = 3,
        limit: int = 20,
    ) -> GraphQueryResult:
        """返回空风险传导路径，并说明图谱未启用。"""

        return _disabled_query_result(self.settings.backend)


class DryRunGraphStore:
    """内存图谱后端，用于 smoke 和工具协议验证。"""

    def __init__(self, settings: GraphStoreSettings | None = None) -> None:
        self.settings = settings or replace(load_graph_store_settings(), dry_run=True)
        self.nodes: dict[str, GraphNode] = {}
        self.relationships: dict[str, GraphRelationship] = {}
        self.mutations: list[GraphMutation] = []

    def initialize_schema(self) -> JsonDict:
        """DryRun 后端无需建库，返回与真实后端一致的初始化协议。"""

        return {
            "backend": self.settings.backend,
            "enabled": self.settings.enabled,
            "dry_run": True,
            "initialized": True,
            "constraints": [],
            "indexes": [],
            "message": "DryRun 后端使用内存图谱，无需初始化真实图数据库。",
        }

    def health_check(self) -> JsonDict:
        """返回内存图谱健康摘要。"""

        return {
            "backend": self.settings.backend,
            "enabled": self.settings.enabled,
            "dry_run": True,
            "healthy": True,
            "node_count": len(self.nodes),
            "relationship_count": len(self.relationships),
        }

    def apply_mutation(self, mutation: GraphMutation) -> GraphSyncResult:
        """把图谱变更合并到内存索引。"""

        self.mutations.append(mutation)
        for node in mutation.nodes:
            self.nodes[node.node_id] = node
        for relationship in mutation.relationships:
            self.relationships[relationship.relationship_id] = relationship
        return GraphSyncResult(
            graph_backend=self.settings.backend,
            node_count=len(mutation.nodes),
            relationship_count=len(mutation.relationships),
            applied=True,
            mutation_id=f"dry-run:{len(self.mutations)}",
            metadata={"dry_run": True, "graph_version": mutation.graph_version},
        )

    def trace_asset_graph(
        self,
        *,
        owner_id: str,
        asset_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> GraphQueryResult:
        """从内存图谱追踪单标的关联路径。"""

        asset_node_id = graph_node_id("Asset", asset_id)
        paths = tuple(
            _bfs_paths(
                nodes=self.nodes,
                relationships=_relationships_for_owner(self.relationships.values(), owner_id),
                start_node_id=asset_node_id,
                max_depth=max(1, min(max_depth, 5)),
                limit=limit,
            )
        )
        node_ids = {node.node_id for path in paths for node in path.nodes}
        relationship_ids = {
            relationship.relationship_id for path in paths for relationship in path.relationships
        }
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            nodes=tuple(self.nodes[node_id] for node_id in node_ids if node_id in self.nodes),
            relationships=tuple(
                self.relationships[relationship_id]
                for relationship_id in relationship_ids
                if relationship_id in self.relationships
            ),
            paths=paths,
            metadata={"dry_run": True, "owner_id": owner_id, "asset_id": asset_id},
        )

    def explain_candidate_reason_chain(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 5,
    ) -> GraphQueryResult:
        """从内存图谱生成入池和持续观察原因链。"""

        chains = _build_candidate_reason_chains(
            nodes=self.nodes,
            relationships=_relationships_for_owner(self.relationships.values(), owner_id),
            owner_id=owner_id,
            asset_id=asset_id,
            limit=limit,
        )
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            chains=tuple(chains),
            metadata={"dry_run": True, "owner_id": owner_id, "asset_id": asset_id},
        )

    def find_memory_conflicts(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 10,
    ) -> GraphQueryResult:
        """在内存图谱里做轻量冲突检测。"""

        memory_nodes = [
            node
            for node in self.nodes.values()
            if "Memory" in node.labels
            and node.properties.get("owner_id") == owner_id
            and (asset_id is None or node.properties.get("asset_id") == asset_id)
        ]
        conflicts = _detect_memory_conflicts(memory_nodes, limit=limit)
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            nodes=tuple(memory_nodes),
            conflicts=tuple(conflicts),
            metadata={"dry_run": True, "owner_id": owner_id, "asset_id": asset_id},
        )

    def find_similar_decision_paths(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 10,
    ) -> GraphQueryResult:
        """按决策类型、动作、风险和证据重叠查找相似历史决策。"""

        relationships = _relationships_for_owner(self.relationships.values(), owner_id)
        paths = _build_similar_decision_paths(
            nodes=self.nodes,
            relationships=relationships,
            owner_id=owner_id,
            asset_id=asset_id,
            limit=limit,
        )
        node_ids = {node.node_id for path in paths for node in path.nodes}
        relationship_ids = {
            relationship.relationship_id for path in paths for relationship in path.relationships
        }
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            nodes=tuple(self.nodes[node_id] for node_id in node_ids if node_id in self.nodes),
            relationships=tuple(
                self.relationships[relationship_id]
                for relationship_id in relationship_ids
                if relationship_id in self.relationships
            ),
            paths=tuple(paths),
            metadata={"dry_run": True, "owner_id": owner_id, "asset_id": asset_id},
        )

    def detect_risk_contagion(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        max_depth: int = 3,
        limit: int = 20,
    ) -> GraphQueryResult:
        """从风险节点出发追踪到资产、证据、决策和记忆的多跳路径。"""

        relationships = _relationships_for_owner(self.relationships.values(), owner_id)
        paths = _build_risk_contagion_paths(
            nodes=self.nodes,
            relationships=relationships,
            asset_id=asset_id,
            max_depth=max(1, min(max_depth, 5)),
            limit=limit,
        )
        node_ids = {node.node_id for path in paths for node in path.nodes}
        relationship_ids = {
            relationship.relationship_id for path in paths for relationship in path.relationships
        }
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            nodes=tuple(self.nodes[node_id] for node_id in node_ids if node_id in self.nodes),
            relationships=tuple(
                self.relationships[relationship_id]
                for relationship_id in relationship_ids
                if relationship_id in self.relationships
            ),
            paths=tuple(paths),
            metadata={
                "dry_run": True,
                "owner_id": owner_id,
                "asset_id": asset_id,
                "max_depth": max_depth,
            },
        )


class Neo4jGraphStore:
    """Neo4j / DozerDB 图谱后端。"""

    def __init__(self, settings: GraphStoreSettings | None = None) -> None:
        self.settings = settings or load_graph_store_settings()
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise RuntimeError(
                "缺少 neo4j Python 驱动。请先安装项目依赖或执行："
                ".venv\\Scripts\\python.exe -m pip install 'neo4j>=5,<6'"
            ) from exc
        self._driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_user, self.settings.neo4j_password),
        )

    def initialize_schema(self) -> JsonDict:
        """创建 Neo4j / DozerDB 约束和索引。"""

        labels = (
            "Asset",
            "Decision",
            "Memory",
            "WatchlistEvent",
            "Risk",
            "Evidence",
            "Review",
        )
        statements: list[str] = []
        with self._driver.session(database=self.settings.neo4j_database) as session:
            for label in labels:
                safe_label = _safe_identifier(label)
                constraint_name = f"finance_{safe_label.lower()}_node_id_unique"
                statement = (
                    f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS "
                    f"FOR (n:{safe_label}) REQUIRE n.node_id IS UNIQUE"
                )
                session.run(statement)
                statements.append(statement)
            for label, property_name in (
                ("Asset", "asset_id"),
                ("Decision", "owner_id"),
                ("Memory", "owner_id"),
                ("WatchlistEvent", "owner_id"),
                ("Risk", "asset_id"),
                ("Evidence", "asset_id"),
                ("Review", "owner_id"),
            ):
                index_name = f"finance_{label.lower()}_{property_name}_idx"
                statement = (
                    f"CREATE INDEX {index_name} IF NOT EXISTS "
                    f"FOR (n:{_safe_identifier(label)}) ON (n.{_safe_identifier(property_name)})"
                )
                session.run(statement)
                statements.append(statement)
            relationship_index = (
                "CREATE INDEX finance_relationship_owner_idx IF NOT EXISTS "
                "FOR ()-[r]-() ON (r.owner_id)"
            )
            session.run(relationship_index)
            statements.append(relationship_index)
        return {
            "backend": self.settings.backend,
            "enabled": self.settings.enabled,
            "dry_run": self.settings.dry_run,
            "initialized": True,
            "neo4j_uri": self.settings.neo4j_uri,
            "neo4j_database": self.settings.neo4j_database,
            "statement_count": len(statements),
            "statements": statements,
        }

    def health_check(self) -> JsonDict:
        """检查 Neo4j / DozerDB 连通性和 GDS 可用性。"""

        with self._driver.session(database=self.settings.neo4j_database) as session:
            ping = session.run("RETURN 1 AS ok").single()
            gds_available = False
            gds_version: str | None = None
            try:
                record = session.run("CALL gds.version() YIELD version RETURN version").single()
                if record is not None:
                    gds_available = True
                    gds_version = str(record["version"])
            except Exception:
                gds_available = False
            counts = session.run(
                "MATCH (n) WITH count(n) AS node_count "
                "MATCH ()-[r]->() RETURN node_count, count(r) AS relationship_count"
            ).single()
        return {
            "backend": self.settings.backend,
            "enabled": self.settings.enabled,
            "healthy": bool(ping and ping["ok"] == 1),
            "neo4j_uri": self.settings.neo4j_uri,
            "neo4j_database": self.settings.neo4j_database,
            "gds_available": gds_available,
            "gds_version": gds_version,
            "node_count": int(counts["node_count"]) if counts else None,
            "relationship_count": int(counts["relationship_count"]) if counts else None,
        }

    def apply_mutation(self, mutation: GraphMutation) -> GraphSyncResult:
        """把图谱变更写入 Neo4j / DozerDB。"""

        with self._driver.session(database=self.settings.neo4j_database) as session:
            for node in mutation.nodes:
                labels = ":".join(_safe_identifier(label) for label in node.labels)
                properties = _neo4j_properties(
                    {
                        **node.properties,
                        "node_id": node.node_id,
                        "labels": list(node.labels),
                        "graph_version": mutation.graph_version,
                    }
                )
                session.run(
                    f"MERGE (n:{labels} {{node_id: $node_id}}) SET n += $properties",
                    node_id=node.node_id,
                    properties=properties,
                )
            for relationship in mutation.relationships:
                relation_type = _safe_identifier(relationship.type)
                properties = _neo4j_properties(
                    {
                        **relationship.properties,
                        "relationship_id": relationship.relationship_id,
                        "type": relationship.type,
                        "graph_version": mutation.graph_version,
                    }
                )
                session.run(
                    "MATCH (s {node_id: $source_node_id}), (t {node_id: $target_node_id}) "
                    f"MERGE (s)-[r:{relation_type} "
                    "{relationship_id: $relationship_id}]->(t) "
                    "SET r += $properties",
                    source_node_id=relationship.source_node_id,
                    target_node_id=relationship.target_node_id,
                    relationship_id=relationship.relationship_id,
                    properties=properties,
                )
        return GraphSyncResult(
            graph_backend=self.settings.backend,
            node_count=len(mutation.nodes),
            relationship_count=len(mutation.relationships),
            applied=True,
            metadata={
                "neo4j_uri": self.settings.neo4j_uri,
                "neo4j_database": self.settings.neo4j_database,
            },
        )

    def trace_asset_graph(
        self,
        *,
        owner_id: str,
        asset_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> GraphQueryResult:
        """从 Neo4j / DozerDB 查询单标的多跳路径。"""

        depth = max(1, min(max_depth, 5))
        query = (
            "MATCH p=(a:Asset {asset_id: $asset_id})-[rels*1.."
            f"{depth}"
            "]-(n) "
            "WHERE all(rel IN relationships(p) WHERE rel.owner_id = $owner_id) "
            "RETURN p LIMIT $limit"
        )
        paths: list[GraphPath] = []
        nodes: dict[str, GraphNode] = {}
        relationships: dict[str, GraphRelationship] = {}
        with self._driver.session(database=self.settings.neo4j_database) as session:
            for record in session.run(query, asset_id=asset_id, owner_id=owner_id, limit=limit):
                path = _neo4j_path_to_graph_path(record["p"], index=len(paths) + 1)
                paths.append(path)
                for node in path.nodes:
                    nodes[node.node_id] = node
                for relationship in path.relationships:
                    relationships[relationship.relationship_id] = relationship
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            nodes=tuple(nodes.values()),
            relationships=tuple(relationships.values()),
            paths=tuple(paths),
            metadata={"owner_id": owner_id, "asset_id": asset_id, "max_depth": depth},
        )

    def explain_candidate_reason_chain(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 5,
    ) -> GraphQueryResult:
        """从 Neo4j / DozerDB 查询入池和持续观察原因链。"""

        query = (
            "MATCH (a:Asset {asset_id: $asset_id})<-[:ABOUT]-(e:WatchlistEvent "
            "{owner_id: $owner_id})-[:SUMMARIZED_BY]->(m:Memory {owner_id: $owner_id}) "
            "OPTIONAL MATCH (d:Decision {owner_id: $owner_id})-[:GENERATES]->(m) "
            "RETURN a, e, m, d ORDER BY e.created_at DESC LIMIT $limit"
        )
        chains: list[JsonDict] = []
        with self._driver.session(database=self.settings.neo4j_database) as session:
            for record in session.run(query, asset_id=asset_id, owner_id=owner_id, limit=limit):
                chains.append(_neo4j_candidate_chain(record))
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            chains=tuple(chains),
            metadata={"owner_id": owner_id, "asset_id": asset_id},
        )

    def find_memory_conflicts(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 10,
    ) -> GraphQueryResult:
        """从 Neo4j / DozerDB 读取记忆后做轻量冲突检测。"""

        if asset_id:
            query = (
                "MATCH (m:Memory {owner_id: $owner_id, asset_id: $asset_id}) "
                "RETURN m ORDER BY m.updated_at DESC LIMIT $limit"
            )
            params = {"owner_id": owner_id, "asset_id": asset_id, "limit": max(limit * 4, 20)}
        else:
            query = (
                "MATCH (m:Memory {owner_id: $owner_id}) "
                "RETURN m ORDER BY m.updated_at DESC LIMIT $limit"
            )
            params = {"owner_id": owner_id, "limit": max(limit * 4, 20)}
        memory_nodes: list[GraphNode] = []
        with self._driver.session(database=self.settings.neo4j_database) as session:
            for record in session.run(query, **params):
                memory_nodes.append(_neo4j_node_to_graph_node(record["m"]))
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            nodes=tuple(memory_nodes),
            conflicts=tuple(_detect_memory_conflicts(memory_nodes, limit=limit)),
            metadata={"owner_id": owner_id, "asset_id": asset_id},
        )

    def find_similar_decision_paths(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 10,
    ) -> GraphQueryResult:
        """从 Neo4j / DozerDB 查询相似历史决策路径。"""

        query = (
            "MATCH (target_asset:Asset {asset_id: $asset_id})<-[:ABOUT]-"
            "(target_decision:Decision {owner_id: $owner_id}) "
            "WITH target_asset, collect(DISTINCT target_decision.decision_type) AS decision_types, "
            "collect(DISTINCT target_decision.suggested_action) AS suggested_actions "
            "MATCH p=(candidate_asset:Asset)<-[:ABOUT]-(candidate_decision:Decision "
            "{owner_id: $owner_id}) "
            "WHERE candidate_asset.asset_id <> $asset_id "
            "AND (candidate_decision.decision_type IN decision_types "
            "OR candidate_decision.suggested_action IN suggested_actions) "
            "OPTIONAL MATCH (candidate_decision)-[:WARNED_BY]->(risk:Risk) "
            "OPTIONAL MATCH (candidate_decision)-[:USES_EVIDENCE]->(evidence:Evidence) "
            "WITH p, candidate_decision, candidate_asset, "
            "count(DISTINCT risk) AS risk_overlap, count(DISTINCT evidence) AS evidence_overlap "
            "RETURN p, (risk_overlap + evidence_overlap) AS overlap_score "
            "ORDER BY overlap_score DESC, candidate_decision.created_at DESC LIMIT $limit"
        )
        paths: list[GraphPath] = []
        nodes: dict[str, GraphNode] = {}
        relationships: dict[str, GraphRelationship] = {}
        with self._driver.session(database=self.settings.neo4j_database) as session:
            for record in session.run(query, owner_id=owner_id, asset_id=asset_id, limit=limit):
                path = _neo4j_path_to_graph_path(record["p"], index=len(paths) + 1)
                score = record.get("overlap_score", 0)
                path = _with_path_summary_prefix(path, f"相似度 {score}: ")
                paths.append(path)
                for node in path.nodes:
                    nodes[node.node_id] = node
                for relationship in path.relationships:
                    relationships[relationship.relationship_id] = relationship
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            nodes=tuple(nodes.values()),
            relationships=tuple(relationships.values()),
            paths=tuple(paths),
            metadata={"owner_id": owner_id, "asset_id": asset_id},
        )

    def detect_risk_contagion(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        max_depth: int = 3,
        limit: int = 20,
    ) -> GraphQueryResult:
        """从 Neo4j / DozerDB 查询风险传导路径。"""

        depth = max(1, min(max_depth, 5))
        asset_filter = "AND (asset_id IS NULL OR any(n IN nodes(p) WHERE n.asset_id = asset_id))"
        query = (
            "MATCH p=(risk:Risk)-[rels*1.."
            f"{depth}"
            "]-(n) "
            "WHERE all(rel IN relationships(p) WHERE rel.owner_id = $owner_id) "
            f"{asset_filter} "
            "RETURN p LIMIT $limit"
        )
        paths: list[GraphPath] = []
        nodes: dict[str, GraphNode] = {}
        relationships: dict[str, GraphRelationship] = {}
        with self._driver.session(database=self.settings.neo4j_database) as session:
            for record in session.run(
                query,
                owner_id=owner_id,
                asset_id=asset_id,
                limit=limit,
            ):
                path = _neo4j_path_to_graph_path(record["p"], index=len(paths) + 1)
                paths.append(path)
                for node in path.nodes:
                    nodes[node.node_id] = node
                for relationship in path.relationships:
                    relationships[relationship.relationship_id] = relationship
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            nodes=tuple(nodes.values()),
            relationships=tuple(relationships.values()),
            paths=tuple(paths),
            metadata={"owner_id": owner_id, "asset_id": asset_id, "max_depth": depth},
        )


class ApacheAgeGraphStore:
    """Apache AGE 图谱后端。

    AGE 和 Neo4j 是二选一部署形态。这里使用 psycopg 直接执行 AGE Cypher，
    不会在失败时自动切换到 Neo4j。
    """

    def __init__(self, settings: GraphStoreSettings | None = None) -> None:
        self.settings = settings or load_graph_store_settings()
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("缺少 psycopg，无法连接 Apache AGE。") from exc
        self._psycopg = psycopg

    def initialize_schema(self) -> JsonDict:
        """初始化 Apache AGE 扩展和图空间。"""

        with self._psycopg.connect(self.settings.age_database_url) as connection:
            self._prepare_age(connection)
            connection.commit()
        return {
            "backend": self.settings.backend,
            "enabled": self.settings.enabled,
            "dry_run": self.settings.dry_run,
            "initialized": True,
            "age_graph_name": self.settings.age_graph_name,
            "age_schema": self.settings.age_schema,
            "message": "Apache AGE 图空间已确认存在。",
        }

    def health_check(self) -> JsonDict:
        """检查 Apache AGE 连通性和图空间状态。"""

        with self._psycopg.connect(self.settings.age_database_url) as connection:
            self._prepare_age(connection)
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                healthy = cursor.fetchone()[0] == 1
                cursor.execute(
                    "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s",
                    (self.settings.age_graph_name,),
                )
                graph_exists = int(cursor.fetchone()[0]) > 0
                try:
                    cursor.execute(self._age_sql("MATCH (n) RETURN count(n)", "node_count agtype"))
                    node_count = str(cursor.fetchone()[0])
                except Exception as exc:
                    node_count = None
                    graph_error = str(exc)
                else:
                    graph_error = None
        return {
            "backend": self.settings.backend,
            "enabled": self.settings.enabled,
            "healthy": healthy and graph_exists,
            "age_graph_name": self.settings.age_graph_name,
            "age_schema": self.settings.age_schema,
            "graph_exists": graph_exists,
            "node_count": node_count,
            "graph_error": graph_error,
        }

    def apply_mutation(self, mutation: GraphMutation) -> GraphSyncResult:
        """把图谱变更写入 Apache AGE。"""

        with self._psycopg.connect(self.settings.age_database_url) as connection:
            self._prepare_age(connection)
            with connection.cursor() as cursor:
                for node in mutation.nodes:
                    labels = ":".join(_safe_identifier(label) for label in node.labels)
                    properties = {
                        **node.properties,
                        "node_id": node.node_id,
                        "labels": list(node.labels),
                        "graph_version": mutation.graph_version,
                    }
                    cypher = (
                        f"MERGE (n:{labels} {{node_id: {_cypher_literal(node.node_id)}}}) "
                        f"SET n += {_cypher_map(properties)} RETURN n"
                    )
                    cursor.execute(self._age_sql(cypher, "n agtype"))
                for relationship in mutation.relationships:
                    relationship_id = _cypher_literal(relationship.relationship_id)
                    properties = {
                        **relationship.properties,
                        "relationship_id": relationship.relationship_id,
                        "type": relationship.type,
                        "graph_version": mutation.graph_version,
                    }
                    cypher = (
                        f"MATCH (s {{node_id: {_cypher_literal(relationship.source_node_id)}}}), "
                        f"(t {{node_id: {_cypher_literal(relationship.target_node_id)}}}) "
                        f"MERGE (s)-[r:{_safe_identifier(relationship.type)} "
                        f"{{relationship_id: {relationship_id}}}]->(t) "
                        f"SET r += {_cypher_map(properties)} RETURN r"
                    )
                    cursor.execute(self._age_sql(cypher, "r agtype"))
            connection.commit()
        return GraphSyncResult(
            graph_backend=self.settings.backend,
            node_count=len(mutation.nodes),
            relationship_count=len(mutation.relationships),
            applied=True,
            metadata={
                "age_graph_name": self.settings.age_graph_name,
                "age_schema": self.settings.age_schema,
            },
        )

    def trace_asset_graph(
        self,
        *,
        owner_id: str,
        asset_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> GraphQueryResult:
        """查询 AGE 原始路径摘要。"""

        depth = max(1, min(max_depth, 5))
        cypher = (
            f"MATCH p=(a:Asset {{asset_id: {_cypher_literal(asset_id)}}})-[*1..{depth}]-(n) "
            f"RETURN p LIMIT {int(limit)}"
        )
        rows = self._query_age(cypher, "p agtype")
        paths = tuple(
            GraphPath(
                path_id=f"age-path:{index}",
                summary="Apache AGE 原始路径，请在上层按需展开。",
                nodes=(),
                relationships=(),
            )
            for index, _row in enumerate(rows, start=1)
        )
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            paths=paths,
            metadata={
                "owner_id": owner_id,
                "asset_id": asset_id,
                "raw_path_count": len(rows),
                "note": "AGE 查询返回 agtype 原始路径，当前工具先返回路径数量和摘要。",
            },
        )

    def explain_candidate_reason_chain(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 5,
    ) -> GraphQueryResult:
        """从 AGE 查询入池原因链摘要。"""

        cypher = (
            f"MATCH (a:Asset {{asset_id: {_cypher_literal(asset_id)}}})"
            f"<-[:ABOUT]-(e:WatchlistEvent {{owner_id: {_cypher_literal(owner_id)}}})"
            f"-[:SUMMARIZED_BY]->(m:Memory {{owner_id: {_cypher_literal(owner_id)}}}) "
            f"RETURN e, m LIMIT {int(limit)}"
        )
        rows = self._query_age(cypher, "e agtype, m agtype")
        chains = tuple(
            {
                "chain_id": f"age-chain:{index}",
                "asset_id": asset_id,
                "summary": "Apache AGE 已返回入池原因链原始 agtype 记录。",
                "raw": json_safe([str(item) for item in row]),
            }
            for index, row in enumerate(rows, start=1)
        )
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            chains=chains,
            metadata={"owner_id": owner_id, "asset_id": asset_id, "raw_chain_count": len(rows)},
        )

    def find_memory_conflicts(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 10,
    ) -> GraphQueryResult:
        """从 AGE 读取记忆原始记录并返回冲突检测摘要。"""

        asset_clause = (
            f", asset_id: {_cypher_literal(asset_id)}"
            if asset_id is not None
            else ""
        )
        cypher = (
            f"MATCH (m:Memory {{owner_id: {_cypher_literal(owner_id)}{asset_clause}}}) "
            f"RETURN m LIMIT {int(max(limit * 4, 20))}"
        )
        rows = self._query_age(cypher, "m agtype")

        return GraphQueryResult(
            graph_backend=self.settings.backend,
            conflicts=(),
            metadata={
                "owner_id": owner_id,
                "asset_id": asset_id,
                "raw_memory_count": len(rows),
                "raw": [str(row[0]) for row in rows[:limit]],
                "note": "Apache AGE 已返回记忆原始记录；冲突语义由上层按同一协议解释。",
                "limit": limit,
            },
        )

    def find_similar_decision_paths(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 10,
    ) -> GraphQueryResult:
        """从 AGE 查询相似历史决策路径摘要。"""

        cypher = (
            f"MATCH (target:Asset {{asset_id: {_cypher_literal(asset_id)}}})"
            f"<-[:ABOUT]-(td:Decision {{owner_id: {_cypher_literal(owner_id)}}}) "
            f"MATCH p=(candidate:Asset)<-[:ABOUT]-(cd:Decision "
            f"{{owner_id: {_cypher_literal(owner_id)}}}) "
            f"WHERE candidate.asset_id <> {_cypher_literal(asset_id)} "
            "AND (cd.decision_type = td.decision_type "
            "OR cd.suggested_action = td.suggested_action) "
            f"RETURN p LIMIT {int(limit)}"
        )
        rows = self._query_age(cypher, "p agtype")
        paths = tuple(
            GraphPath(
                path_id=f"age-similar-decision:{index}",
                summary="Apache AGE 已返回相似历史决策路径原始 agtype 记录。",
                nodes=(),
                relationships=(),
            )
            for index, _row in enumerate(rows, start=1)
        )
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            paths=paths,
            metadata={
                "owner_id": owner_id,
                "asset_id": asset_id,
                "raw_path_count": len(rows),
                "raw": [str(row[0]) for row in rows[:limit]],
            },
        )

    def detect_risk_contagion(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        max_depth: int = 3,
        limit: int = 20,
    ) -> GraphQueryResult:
        """从 AGE 查询风险传导路径摘要。"""

        depth = max(1, min(max_depth, 5))
        asset_where = (
            f"WHERE n.asset_id = {_cypher_literal(asset_id)} "
            if asset_id is not None
            else ""
        )
        cypher = (
            f"MATCH p=(risk:Risk)-[*1..{depth}]-(n) "
            f"{asset_where}"
            f"RETURN p LIMIT {int(limit)}"
        )
        rows = self._query_age(cypher, "p agtype")
        paths = tuple(
            GraphPath(
                path_id=f"age-risk-contagion:{index}",
                summary="Apache AGE 已返回风险传导路径原始 agtype 记录。",
                nodes=(),
                relationships=(),
            )
            for index, _row in enumerate(rows, start=1)
        )
        return GraphQueryResult(
            graph_backend=self.settings.backend,
            paths=paths,
            metadata={
                "owner_id": owner_id,
                "asset_id": asset_id,
                "max_depth": depth,
                "raw_path_count": len(rows),
                "raw": [str(row[0]) for row in rows[:limit]],
            },
        )

    def _prepare_age(self, connection: Any) -> None:
        """初始化 AGE 扩展和图空间。"""

        with connection.cursor() as cursor:
            cursor.execute("LOAD 'age'")
            cursor.execute(f"SET search_path = {self.settings.age_schema}, \"$user\", public")
            cursor.execute(
                "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s",
                (self.settings.age_graph_name,),
            )
            exists = int(cursor.fetchone()[0]) > 0
            if not exists:
                cursor.execute("SELECT create_graph(%s)", (self.settings.age_graph_name,))

    def _age_sql(self, cypher: str, columns: str) -> str:
        """构造 AGE Cypher SQL。"""

        return (
            f"SELECT * FROM cypher('{self.settings.age_graph_name}', "
            f"$${cypher}$$) AS ({columns})"
        )

    def _query_age(self, cypher: str, columns: str) -> list[tuple[Any, ...]]:
        """执行 AGE 查询并返回原始行。"""

        with self._psycopg.connect(self.settings.age_database_url) as connection:
            self._prepare_age(connection)
            with connection.cursor() as cursor:
                cursor.execute(self._age_sql(cypher, columns))
                return list(cursor.fetchall())


def create_graph_store(settings: GraphStoreSettings | None = None) -> MemoryGraphStore:
    """按配置创建唯一 GraphStore 后端。"""

    resolved = settings or load_graph_store_settings()
    if not resolved.enabled:
        return DisabledGraphStore(resolved)
    if resolved.dry_run:
        return DryRunGraphStore(resolved)
    if resolved.backend == "neo4j":
        return Neo4jGraphStore(resolved)
    if resolved.backend == "apache_age":
        return ApacheAgeGraphStore(resolved)
    raise ValueError(f"不支持的 GraphStore 后端: {resolved.backend}")


def _disabled_query_result(backend: str) -> GraphQueryResult:
    return GraphQueryResult(
        graph_backend=backend,
        metadata={"enabled": False, "warning": "GraphStore 未启用。"},
    )


def _relationships_for_owner(
    relationships: Iterable[GraphRelationship],
    owner_id: str,
) -> tuple[GraphRelationship, ...]:
    return tuple(
        relationship
        for relationship in relationships
        if relationship.properties.get("owner_id") == owner_id
    )


def _bfs_paths(
    *,
    nodes: dict[str, GraphNode],
    relationships: tuple[GraphRelationship, ...],
    start_node_id: str,
    max_depth: int,
    limit: int,
) -> list[GraphPath]:
    if start_node_id not in nodes:
        return []
    adjacency: dict[str, list[GraphRelationship]] = {}
    for relationship in relationships:
        adjacency.setdefault(relationship.source_node_id, []).append(relationship)
        adjacency.setdefault(relationship.target_node_id, []).append(relationship)

    result: list[GraphPath] = []
    queue: deque[tuple[str, tuple[str, ...], tuple[GraphRelationship, ...]]] = deque(
        [(start_node_id, (start_node_id,), ())]
    )
    while queue and len(result) < limit:
        current_node_id, path_node_ids, path_relationships = queue.popleft()
        if path_relationships:
            path_nodes = tuple(nodes[node_id] for node_id in path_node_ids if node_id in nodes)
            result.append(
                GraphPath(
                    path_id=f"path:{len(result) + 1}",
                    summary=_path_summary(path_nodes, path_relationships),
                    nodes=path_nodes,
                    relationships=path_relationships,
                )
            )
        if len(path_relationships) >= max_depth:
            continue
        for relationship in adjacency.get(current_node_id, []):
            next_node_id = (
                relationship.target_node_id
                if relationship.source_node_id == current_node_id
                else relationship.source_node_id
            )
            if next_node_id in path_node_ids:
                continue
            queue.append(
                (
                    next_node_id,
                    (*path_node_ids, next_node_id),
                    (*path_relationships, relationship),
                )
            )
    return result


def _path_summary(
    nodes: tuple[GraphNode, ...],
    relationships: tuple[GraphRelationship, ...],
) -> str:
    labels = [node.labels[0] if node.labels else "Node" for node in nodes]
    rels = [relationship.type for relationship in relationships]
    parts: list[str] = []
    for index, label in enumerate(labels):
        parts.append(label)
        if index < len(rels):
            parts.append(f"-[{rels[index]}]-")
    return " ".join(parts)


def _build_candidate_reason_chains(
    *,
    nodes: dict[str, GraphNode],
    relationships: tuple[GraphRelationship, ...],
    owner_id: str,
    asset_id: str,
    limit: int,
) -> list[JsonDict]:
    asset_node_id = graph_node_id("Asset", asset_id)
    by_source: dict[str, list[GraphRelationship]] = {}
    by_target: dict[str, list[GraphRelationship]] = {}
    for relationship in relationships:
        by_source.setdefault(relationship.source_node_id, []).append(relationship)
        by_target.setdefault(relationship.target_node_id, []).append(relationship)

    chains: list[JsonDict] = []
    for event_node in nodes.values():
        if "WatchlistEvent" not in event_node.labels:
            continue
        if event_node.properties.get("owner_id") != owner_id:
            continue
        if event_node.properties.get("asset_id") != asset_id:
            continue
        about_asset = any(
            relationship.type == "ABOUT" and relationship.target_node_id == asset_node_id
            for relationship in by_source.get(event_node.node_id, [])
        )
        if not about_asset:
            continue
        memory_relationships = [
            relationship
            for relationship in by_source.get(event_node.node_id, [])
            if relationship.type == "SUMMARIZED_BY"
        ]
        for relationship in memory_relationships:
            memory_node = nodes.get(relationship.target_node_id)
            if memory_node is None:
                continue
            decision_node = _find_decision_for_memory(
                nodes=nodes,
                incoming=by_target.get(memory_node.node_id, []),
            )
            chains.append(
                {
                    "chain_id": f"chain:{len(chains) + 1}",
                    "asset_id": asset_id,
                    "event_id": event_node.properties.get("event_id"),
                    "event_type": event_node.properties.get("event_type"),
                    "event_reason": event_node.properties.get("reason"),
                    "memory_id": memory_node.properties.get("memory_id"),
                    "memory_type": memory_node.properties.get("memory_type"),
                    "memory_content": memory_node.properties.get("content"),
                    "decision_id": (
                        decision_node.properties.get("decision_id") if decision_node else None
                    ),
                    "decision_summary": (
                        decision_node.properties.get("summary") if decision_node else None
                    ),
                    "summary": _candidate_chain_summary(event_node, memory_node, decision_node),
                }
            )
            if len(chains) >= limit:
                return chains
    return chains


def _build_similar_decision_paths(
    *,
    nodes: dict[str, GraphNode],
    relationships: tuple[GraphRelationship, ...],
    owner_id: str,
    asset_id: str,
    limit: int,
) -> list[GraphPath]:
    """基于图结构查找与目标标的相似的历史决策路径。"""

    target_asset_node_id = graph_node_id("Asset", asset_id)
    outgoing, incoming = _relationship_indexes(relationships)
    target_decisions = [
        nodes[relationship.source_node_id]
        for relationship in incoming.get(target_asset_node_id, [])
        if relationship.type == "ABOUT"
        and relationship.source_node_id in nodes
        and "Decision" in nodes[relationship.source_node_id].labels
        and nodes[relationship.source_node_id].properties.get("owner_id") == owner_id
    ]
    target_profile = _decision_profile(target_decisions, outgoing)
    if not target_profile:
        return []

    scored_paths: list[tuple[int, GraphPath]] = []
    for relationship in relationships:
        if relationship.type != "ABOUT":
            continue
        decision = nodes.get(relationship.source_node_id)
        candidate_asset = nodes.get(relationship.target_node_id)
        if decision is None or candidate_asset is None:
            continue
        if "Decision" not in decision.labels or "Asset" not in candidate_asset.labels:
            continue
        if decision.properties.get("owner_id") != owner_id:
            continue
        if candidate_asset.properties.get("asset_id") == asset_id:
            continue
        score = _decision_similarity_score(decision, outgoing, target_profile)
        if score <= 0:
            continue
        path_nodes = [decision, candidate_asset]
        path_relationships = [relationship]
        detail_relationship = _first_detail_relationship(
            outgoing.get(decision.node_id, []),
            types=("WARNED_BY", "USES_EVIDENCE", "GENERATES"),
        )
        if detail_relationship:
            detail_node = nodes.get(detail_relationship.target_node_id)
            if detail_node:
                path_nodes.append(detail_node)
                path_relationships.append(detail_relationship)
        summary = _path_summary(tuple(path_nodes), tuple(path_relationships))
        path = GraphPath(
            path_id=f"similar-decision:{len(scored_paths) + 1}",
            summary=f"相似度 {score}: {summary}",
            nodes=tuple(path_nodes),
            relationships=tuple(path_relationships),
        )
        scored_paths.append((score, path))

    scored_paths.sort(
        key=lambda item: (
            item[0],
            str(item[1].nodes[0].properties.get("created_at") or ""),
        ),
        reverse=True,
    )
    return [path for _score, path in scored_paths[:limit]]


def _build_risk_contagion_paths(
    *,
    nodes: dict[str, GraphNode],
    relationships: tuple[GraphRelationship, ...],
    asset_id: str | None,
    max_depth: int,
    limit: int,
) -> list[GraphPath]:
    """从风险节点出发构造风险传导路径。"""

    risk_nodes = [
        node
        for node in nodes.values()
        if "Risk" in node.labels
        and (asset_id is None or node.properties.get("asset_id") == asset_id)
    ]
    paths: list[GraphPath] = []
    for risk_node in risk_nodes:
        for path in _bfs_paths(
            nodes=nodes,
            relationships=relationships,
            start_node_id=risk_node.node_id,
            max_depth=max_depth,
            limit=max(limit - len(paths), 0),
        ):
            if asset_id is not None and not any(
                node.properties.get("asset_id") == asset_id for node in path.nodes
            ):
                continue
            paths.append(
                GraphPath(
                    path_id=f"risk-contagion:{len(paths) + 1}",
                    summary=path.summary,
                    nodes=path.nodes,
                    relationships=path.relationships,
                )
            )
            if len(paths) >= limit:
                return paths
    return paths


def _relationship_indexes(
    relationships: tuple[GraphRelationship, ...],
) -> tuple[dict[str, list[GraphRelationship]], dict[str, list[GraphRelationship]]]:
    """构建出边和入边索引。"""

    outgoing: dict[str, list[GraphRelationship]] = {}
    incoming: dict[str, list[GraphRelationship]] = {}
    for relationship in relationships:
        outgoing.setdefault(relationship.source_node_id, []).append(relationship)
        incoming.setdefault(relationship.target_node_id, []).append(relationship)
    return outgoing, incoming


def _decision_profile(
    decisions: list[GraphNode],
    outgoing: dict[str, list[GraphRelationship]],
) -> JsonDict:
    """提取目标决策的结构化画像。"""

    if not decisions:
        return {}
    profile: JsonDict = {
        "decision_types": set(),
        "suggested_actions": set(),
        "risk_ids": set(),
        "evidence_ids": set(),
    }
    for decision in decisions:
        profile["decision_types"].add(decision.properties.get("decision_type"))
        profile["suggested_actions"].add(decision.properties.get("suggested_action"))
        profile["risk_ids"].update(decision.properties.get("risk_ids") or [])
        profile["evidence_ids"].update(decision.properties.get("evidence_ids") or [])
        for relationship in outgoing.get(decision.node_id, []):
            if relationship.type == "WARNED_BY":
                profile["risk_ids"].add(_raw_id_from_node_id(relationship.target_node_id))
            if relationship.type == "USES_EVIDENCE":
                profile["evidence_ids"].add(_raw_id_from_node_id(relationship.target_node_id))
    return {key: {item for item in value if item} for key, value in profile.items()}


def _decision_similarity_score(
    decision: GraphNode,
    outgoing: dict[str, list[GraphRelationship]],
    target_profile: JsonDict,
) -> int:
    """计算候选决策与目标画像的轻量结构相似度。"""

    score = 0
    if decision.properties.get("decision_type") in target_profile.get("decision_types", set()):
        score += 3
    if decision.properties.get("suggested_action") in target_profile.get(
        "suggested_actions", set()
    ):
        score += 2
    risk_ids = set(decision.properties.get("risk_ids") or [])
    evidence_ids = set(decision.properties.get("evidence_ids") or [])
    for relationship in outgoing.get(decision.node_id, []):
        if relationship.type == "WARNED_BY":
            risk_ids.add(_raw_id_from_node_id(relationship.target_node_id))
        if relationship.type == "USES_EVIDENCE":
            evidence_ids.add(_raw_id_from_node_id(relationship.target_node_id))
    score += min(3, len(risk_ids & target_profile.get("risk_ids", set())))
    score += min(3, len(evidence_ids & target_profile.get("evidence_ids", set())))
    return score


def _first_detail_relationship(
    relationships: list[GraphRelationship],
    *,
    types: tuple[str, ...],
) -> GraphRelationship | None:
    """选取一条能解释相似性的详情边。"""

    for relation_type in types:
        for relationship in relationships:
            if relationship.type == relation_type:
                return relationship
    return None


def _raw_id_from_node_id(node_id: str) -> str:
    """从稳定节点 ID 中取回原始 ID。"""

    return node_id.split(":", 1)[1] if ":" in node_id else node_id


def _find_decision_for_memory(
    *,
    nodes: dict[str, GraphNode],
    incoming: list[GraphRelationship],
) -> GraphNode | None:
    for relationship in incoming:
        if relationship.type == "GENERATES":
            node = nodes.get(relationship.source_node_id)
            if node and "Decision" in node.labels:
                return node
    return None


def _candidate_chain_summary(
    event_node: GraphNode,
    memory_node: GraphNode,
    decision_node: GraphNode | None,
) -> str:
    event_reason = event_node.properties.get("reason") or event_node.properties.get("event_type")
    memory_content = memory_node.properties.get("content")
    decision_summary = decision_node.properties.get("summary") if decision_node else None
    parts = [str(part) for part in (event_reason, memory_content, decision_summary) if part]
    return " | ".join(parts)


def _detect_memory_conflicts(memory_nodes: list[GraphNode], *, limit: int) -> list[JsonDict]:
    positive_nodes = [node for node in memory_nodes if _memory_polarity(node) == "positive"]
    negative_nodes = [node for node in memory_nodes if _memory_polarity(node) == "negative"]
    conflicts: list[JsonDict] = []
    for positive in positive_nodes:
        for negative in negative_nodes:
            if positive.node_id == negative.node_id:
                continue
            conflicts.append(
                {
                    "conflict_id": f"conflict:{len(conflicts) + 1}",
                    "asset_id": positive.properties.get("asset_id")
                    or negative.properties.get("asset_id"),
                    "positive_memory_id": positive.properties.get("memory_id"),
                    "negative_memory_id": negative.properties.get("memory_id"),
                    "positive_content": positive.properties.get("content"),
                    "negative_content": negative.properties.get("content"),
                    "summary": "同一标的同时存在看多/纳入观察与失效/卖出类记忆，需要 Agent 复核。",
                }
            )
            if len(conflicts) >= limit:
                return conflicts
    return conflicts


def _memory_polarity(node: GraphNode) -> str:
    memory_type = str(node.properties.get("memory_type") or "").lower()
    content = str(node.properties.get("content") or "").lower()
    positive_keywords = ("candidate", "intake", "watch", "buy", "纳入", "入池", "看好", "买入")
    negative_keywords = (
        "remove",
        "invalid",
        "risk",
        "sell",
        "stop",
        "剔除",
        "失效",
        "卖出",
        "止损",
        "风险",
    )
    if any(keyword in memory_type or keyword in content for keyword in negative_keywords):
        return "negative"
    if any(keyword in memory_type or keyword in content for keyword in positive_keywords):
        return "positive"
    return "neutral"


def _safe_identifier(value: str) -> str:
    identifier = re.sub(r"[^0-9A-Za-z_]", "_", value)
    if not identifier:
        return "Unknown"
    if identifier[0].isdigit():
        return f"_{identifier}"
    return identifier


def _neo4j_properties(properties: JsonDict) -> JsonDict:
    safe: JsonDict = {}
    for key, value in properties.items():
        safe_key = _safe_identifier(str(key))
        converted = json_safe(value)
        if isinstance(converted, dict):
            safe[f"{safe_key}_json"] = compact_json(converted)
        elif isinstance(converted, list):
            if all(
                item is None or isinstance(item, str | bool | int | float)
                for item in converted
            ):
                safe[safe_key] = converted
            else:
                safe[f"{safe_key}_json"] = compact_json(converted)
        else:
            safe[safe_key] = converted
    return safe


def _neo4j_node_to_graph_node(node: Any) -> GraphNode:
    properties = dict(node)
    node_id = properties.get("node_id") or node.element_id
    return GraphNode(node_id=str(node_id), labels=tuple(node.labels), properties=properties)


def _neo4j_relationship_to_graph_relationship(
    relationship: Any,
    *,
    source_node: Any | None = None,
    target_node: Any | None = None,
) -> GraphRelationship:
    properties = dict(relationship)
    relationship_id = properties.get("relationship_id") or relationship.element_id
    source_node_id = _neo4j_node_property(source_node, "node_id")
    target_node_id = _neo4j_node_property(target_node, "node_id")
    if source_node_id is None and hasattr(relationship, "start_node"):
        source_node_id = _neo4j_node_property(relationship.start_node, "node_id")
    if target_node_id is None and hasattr(relationship, "end_node"):
        target_node_id = _neo4j_node_property(relationship.end_node, "node_id")
    return GraphRelationship(
        relationship_id=str(relationship_id),
        type=relationship.type,
        source_node_id=str(source_node_id or relationship.start_node.element_id),
        target_node_id=str(target_node_id or relationship.end_node.element_id),
        properties=properties,
    )


def _neo4j_path_to_graph_path(path: Any, *, index: int) -> GraphPath:
    nodes = tuple(_neo4j_node_to_graph_node(node) for node in path.nodes)
    relationships = tuple(
        _neo4j_relationship_to_graph_relationship(
            relationship,
            source_node=path.nodes[relation_index],
            target_node=path.nodes[relation_index + 1],
        )
        for relation_index, relationship in enumerate(path.relationships)
    )
    return GraphPath(
        path_id=f"neo4j-path:{index}",
        summary=_path_summary(nodes, relationships),
        nodes=nodes,
        relationships=relationships,
    )


def _with_path_summary_prefix(path: GraphPath, prefix: str) -> GraphPath:
    """复制路径并增加摘要前缀。"""

    return GraphPath(
        path_id=path.path_id,
        summary=f"{prefix}{path.summary}",
        nodes=path.nodes,
        relationships=path.relationships,
    )


def _neo4j_candidate_chain(record: Any) -> JsonDict:
    event = _neo4j_node_to_graph_node(record["e"])
    memory = _neo4j_node_to_graph_node(record["m"])
    decision = _neo4j_node_to_graph_node(record["d"]) if record["d"] is not None else None
    return {
        "chain_id": f"neo4j-chain:{event.properties.get('event_id')}",
        "asset_id": memory.properties.get("asset_id") or event.properties.get("asset_id"),
        "event_id": event.properties.get("event_id"),
        "event_type": event.properties.get("event_type"),
        "event_reason": event.properties.get("reason"),
        "memory_id": memory.properties.get("memory_id"),
        "memory_type": memory.properties.get("memory_type"),
        "memory_content": memory.properties.get("content"),
        "decision_id": decision.properties.get("decision_id") if decision else None,
        "decision_summary": decision.properties.get("summary") if decision else None,
        "summary": _candidate_chain_summary(event, memory, decision),
    }


def _neo4j_node_property(node: Any | None, key: str) -> Any | None:
    if node is None:
        return None
    if hasattr(node, "get"):
        return node.get(key)
    try:
        return dict(node).get(key)
    except Exception:
        return None


def _cypher_literal(value: Any) -> str:
    converted = json_safe(value)
    if converted is None:
        return "null"
    if isinstance(converted, bool):
        return "true" if converted else "false"
    if isinstance(converted, int | float):
        return str(converted)
    if isinstance(converted, list):
        return "[" + ", ".join(_cypher_literal(item) for item in converted) + "]"
    if isinstance(converted, dict):
        return _cypher_map(converted)
    text = str(converted).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def _cypher_map(value: JsonDict) -> str:
    items = []
    for key, item in json_safe(value).items():
        safe_key = _safe_identifier(str(key))
        if isinstance(item, dict | list):
            items.append(f"{safe_key}: {_cypher_literal(compact_json(item))}")
        else:
            items.append(f"{safe_key}: {_cypher_literal(item)}")
    return "{" + ", ".join(items) + "}"
