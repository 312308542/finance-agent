"""知识图谱 GraphStore 的领域模型。

这些模型只描述从 PostgreSQL 事实库投影到图数据库的结构化变更，
不把图数据库作为金融事实源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

JsonDict = dict[str, Any]


def json_safe(value: Any) -> Any:
    """转换为 JSON 和图数据库属性都容易处理的值。"""

    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe(item) for item in value]
    return str(value)


def graph_node_id(label: str, raw_id: str) -> str:
    """生成稳定图节点 ID。"""

    return f"{label.lower()}:{raw_id}"


def graph_relationship_id(
    relation_type: str,
    source_node_id: str,
    target_node_id: str,
) -> str:
    """生成稳定图关系 ID。"""

    return f"{relation_type.lower()}:{source_node_id}->{target_node_id}"


@dataclass(frozen=True)
class GraphNode:
    """图节点。"""

    node_id: str
    labels: tuple[str, ...]
    properties: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        """序列化为工具返回协议。"""

        return {
            "node_id": self.node_id,
            "labels": list(self.labels),
            "properties": json_safe(self.properties),
        }


@dataclass(frozen=True)
class GraphRelationship:
    """图关系。"""

    relationship_id: str
    type: str
    source_node_id: str
    target_node_id: str
    properties: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        """序列化为工具返回协议。"""

        return {
            "relationship_id": self.relationship_id,
            "type": self.type,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "properties": json_safe(self.properties),
        }


@dataclass(frozen=True)
class GraphMutation:
    """一次图谱投影变更。"""

    owner_id: str
    asset_id: str | None
    nodes: tuple[GraphNode, ...]
    relationships: tuple[GraphRelationship, ...]
    graph_version: str = "finance-memory-graph-v1"
    generated_at: datetime | None = None

    def to_dict(self) -> JsonDict:
        """序列化为工具返回协议。"""

        return {
            "owner_id": self.owner_id,
            "asset_id": self.asset_id,
            "graph_version": self.graph_version,
            "generated_at": json_safe(self.generated_at),
            "nodes": [node.to_dict() for node in self.nodes],
            "relationships": [relationship.to_dict() for relationship in self.relationships],
        }


@dataclass(frozen=True)
class GraphPath:
    """图查询返回的一条路径摘要。"""

    path_id: str
    summary: str
    nodes: tuple[GraphNode, ...]
    relationships: tuple[GraphRelationship, ...]

    def to_dict(self) -> JsonDict:
        """序列化为工具返回协议。"""

        return {
            "path_id": self.path_id,
            "summary": self.summary,
            "nodes": [node.to_dict() for node in self.nodes],
            "relationships": [relationship.to_dict() for relationship in self.relationships],
        }


@dataclass(frozen=True)
class GraphQueryResult:
    """图查询结构化结果。"""

    graph_backend: str
    nodes: tuple[GraphNode, ...] = ()
    relationships: tuple[GraphRelationship, ...] = ()
    paths: tuple[GraphPath, ...] = ()
    chains: tuple[JsonDict, ...] = ()
    conflicts: tuple[JsonDict, ...] = ()
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        """序列化为工具返回协议。"""

        return {
            "graph_backend": self.graph_backend,
            "nodes": [node.to_dict() for node in self.nodes],
            "relationships": [relationship.to_dict() for relationship in self.relationships],
            "paths": [path.to_dict() for path in self.paths],
            "chains": [json_safe(chain) for chain in self.chains],
            "conflicts": [json_safe(conflict) for conflict in self.conflicts],
            "metadata": json_safe(self.metadata),
        }


@dataclass(frozen=True)
class GraphSyncResult:
    """图谱同步结果。"""

    graph_backend: str
    node_count: int
    relationship_count: int
    applied: bool
    mutation_id: str | None = None
    warnings: tuple[str, ...] = ()
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        """序列化为工具返回协议。"""

        return {
            "graph_backend": self.graph_backend,
            "node_count": self.node_count,
            "relationship_count": self.relationship_count,
            "applied": self.applied,
            "mutation_id": self.mutation_id,
            "warnings": list(self.warnings),
            "metadata": json_safe(self.metadata),
        }


def compact_json(value: Any) -> str:
    """生成稳定紧凑 JSON 文本，用于图数据库属性兜底保存。"""

    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
