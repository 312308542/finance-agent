"""知识图谱后端配置。

同一运行环境只能显式选择一个图数据库后端：Neo4j / DozerDB 或 Apache AGE。
这里不做自动 fallback，也不做双写双读。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from tomllib import loads as load_toml
from typing import Literal

from finance_agent.storage.db import get_database_url

GraphBackend = Literal["neo4j", "apache_age"]


@dataclass(frozen=True)
class GraphStoreSettings:
    """GraphStore 运行配置。"""

    enabled: bool
    backend: GraphBackend
    dry_run: bool
    graph_version: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str
    age_database_url: str
    age_graph_name: str
    age_schema: str


def parse_bool(value: object, *, default: bool = False) -> bool:
    """解析环境变量布尔值。"""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_graph_store_settings(config_file: str | None = None) -> GraphStoreSettings:
    """读取 GraphStore 配置。

    优先使用显式传入的配置文件，其次读取 `FINANCE_AGENT_GRAPH_CONFIG_FILE`，
    最后使用环境变量。环境变量仍可覆盖配置文件中的敏感连接信息。
    """

    file_payload = load_graph_config_file(
        config_file or os.getenv("FINANCE_AGENT_GRAPH_CONFIG_FILE")
    )
    graph_payload = file_payload.get("graph", file_payload)

    backend = _config_value(
        graph_payload,
        "backend",
        env_name="FINANCE_AGENT_GRAPH_BACKEND",
        default="neo4j",
    )
    backend = str(backend).strip().lower()
    if backend not in {"neo4j", "apache_age"}:
        raise ValueError(
            "FINANCE_AGENT_GRAPH_BACKEND 只能配置为 neo4j 或 apache_age，"
            f"当前值为 {backend!r}。"
        )

    neo4j_payload = _section(graph_payload, "neo4j")
    age_payload = _section(graph_payload, "apache_age")

    enabled = _config_value(
        graph_payload,
        "enabled",
        env_name="FINANCE_AGENT_GRAPH_ENABLED",
        default=False,
    )
    dry_run = _config_value(
        graph_payload,
        "dry_run",
        env_name="FINANCE_AGENT_GRAPH_DRY_RUN",
        default=False,
    )
    return GraphStoreSettings(
        enabled=parse_bool(enabled, default=False),
        backend=backend,  # type: ignore[arg-type]
        dry_run=parse_bool(dry_run, default=False),
        graph_version=str(
            _config_value(
                graph_payload,
                "graph_version",
                env_name="FINANCE_AGENT_GRAPH_VERSION",
                default="finance-memory-graph-v1",
            )
        ),
        neo4j_uri=str(
            _config_value(
                neo4j_payload,
                "uri",
                env_name="FINANCE_AGENT_NEO4J_URI",
                default="bolt://localhost:7687",
            )
        ),
        neo4j_user=str(
            _config_value(
                neo4j_payload,
                "user",
                env_name="FINANCE_AGENT_NEO4J_USER",
                default="neo4j",
            )
        ),
        neo4j_password=str(
            _config_value(
                neo4j_payload,
                "password",
                env_name="FINANCE_AGENT_NEO4J_PASSWORD",
                default="password123",
            )
        ),
        neo4j_database=str(
            _config_value(
                neo4j_payload,
                "database",
                env_name="FINANCE_AGENT_NEO4J_DATABASE",
                default="neo4j",
            )
        ),
        age_database_url=str(
            _config_value(
                age_payload,
                "database_url",
                env_name="FINANCE_AGENT_AGE_DATABASE_URL",
                default=get_database_url(),
            )
        ),
        age_graph_name=str(
            _config_value(
                age_payload,
                "graph_name",
                env_name="FINANCE_AGENT_AGE_GRAPH_NAME",
                default="finance_memory_graph",
            )
        ),
        age_schema=str(
            _config_value(
                age_payload,
                "schema",
                env_name="FINANCE_AGENT_AGE_SCHEMA",
                default="ag_catalog",
            )
        ),
    )


def load_graph_config_file(config_file: str | None) -> dict[str, object]:
    """读取 TOML 或 JSON 图谱配置文件。"""

    if not config_file:
        return {}
    path = Path(config_file)
    payload_text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(payload_text)
    else:
        payload = load_toml(payload_text)
    if not isinstance(payload, dict):
        raise ValueError("图谱配置文件必须是对象。")
    return payload


def _section(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _config_value(
    payload: dict[str, object],
    key: str,
    *,
    env_name: str,
    default: object,
) -> object:
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value
    return payload.get(key, default)
