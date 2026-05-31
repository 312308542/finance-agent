"""测试数据清理工具。

本模块只清理带有明确 smoke/test 标记的数据，不按股票代码或聊天正文全文删除，
避免误删真实采集数据和用户历史对话。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import RowMapping

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class CleanupSpec:
    """单表清理规则。"""

    table: str
    id_columns: tuple[str, ...] = ()
    source_columns: tuple[str, ...] = ()
    payload_source: bool = True
    payload_text: bool = False
    note: str = ""
    extra_conditions: tuple[str, ...] = ()
    disabled: bool = False


@dataclass(frozen=True)
class CleanupTableResult:
    """单表清理结果。"""

    table: str
    matched: int
    deleted: int
    backup_file: str | None


@dataclass(frozen=True)
class CleanupResult:
    """一次清理运行结果。"""

    backup_dir: str
    dry_run: bool
    started_at: str
    finished_at: str
    tables: tuple[CleanupTableResult, ...]

    @property
    def matched_total(self) -> int:
        return sum(item.matched for item in self.tables)

    @property
    def deleted_total(self) -> int:
        return sum(item.deleted for item in self.tables)


@dataclass(frozen=True)
class DeletePlan:
    """包含备份查询和删除语句的单表执行计划。"""

    spec: CleanupSpec
    where_clause: str
    query_sql: str
    delete_sql: str


DEFAULT_BACKUP_ROOT = Path("runtime/backups")


# 顺序按依赖关系从子表到父表排列，最后才清资产主表。
DEFAULT_CLEANUP_SPECS: tuple[CleanupSpec, ...] = (
    CleanupSpec(
        "agent_workflow_events",
        id_columns=("workflow_event_id", "workflow_run_id"),
        payload_text=True,
    ),
    CleanupSpec(
        "assistant_chat_messages",
        id_columns=("chat_message_id", "chat_session_id", "owner_id"),
        payload_text=True,
    ),
    CleanupSpec("memory_embeddings", id_columns=("embedding_id", "owner_id", "memory_id", "source_id")),
    CleanupSpec(
        "financial_memory_edges",
        id_columns=("edge_id", "owner_id", "source_id", "target_id"),
        payload_text=True,
    ),
    CleanupSpec(
        "watchlist_item_events",
        id_columns=(
            "event_id",
            "owner_id",
            "watchlist_id",
            "watchlist_item_id",
            "asset_id",
            "source_decision_id",
        ),
    ),
    CleanupSpec("position_snapshots", id_columns=("snapshot_id", "position_id", "portfolio_id", "asset_id"), source_columns=("source",)),
    CleanupSpec("portfolio_snapshots", id_columns=("snapshot_id", "portfolio_id", "owner_id"), source_columns=("source",)),
    CleanupSpec("positions", id_columns=("position_id", "portfolio_id", "asset_id")),
    CleanupSpec("watchlist_items", id_columns=("watchlist_item_id", "watchlist_id", "asset_id", "source_id")),
    CleanupSpec("asset_theses", id_columns=("thesis_id", "asset_id", "owner_id", "source_id")),
    CleanupSpec("monitoring_alerts", id_columns=("alert_id", "owner_id", "portfolio_id", "asset_id")),
    CleanupSpec("decision_logs", id_columns=("decision_id", "owner_id", "portfolio_id", "asset_id", "source_recommendation_id", "source_alert_id", "workflow_run_id"), source_columns=("decision_type",)),
    CleanupSpec("assistant_memories", id_columns=("memory_id", "owner_id", "asset_id", "source_decision_id", "source_review_task_id")),
    CleanupSpec("review_tasks", id_columns=("review_task_id", "owner_id", "asset_id", "source_decision_id")),
    CleanupSpec("assistant_trigger_events", id_columns=("trigger_event_id", "owner_id", "agent_task_id", "portfolio_id", "watchlist_id", "recommendation_run_id", "asset_id")),
    CleanupSpec("assistant_chat_sessions", id_columns=("chat_session_id", "owner_id"), payload_text=True),
    CleanupSpec("agent_workflow_runs", id_columns=("workflow_run_id", "owner_id"), source_columns=("workflow_type",)),
    CleanupSpec("agent_analysis_items", id_columns=("agent_analysis_item_id", "agent_run_id", "run_id", "asset_id")),
    CleanupSpec("agent_analysis_runs", id_columns=("agent_run_id", "run_id")),
    CleanupSpec("asset_recommendations", id_columns=("recommendation_id", "run_id", "asset_id", "score_id", "factor_frame_id")),
    CleanupSpec("recommendation_run_universes", id_columns=("run_id", "universe_id")),
    CleanupSpec("recommendation_runs", id_columns=("run_id", "universe_id", "screening_id"), source_columns=("strategy",), payload_text=True),
    CleanupSpec("asset_scores", id_columns=("score_id", "asset_id", "universe_id", "screening_id", "factor_frame_id"), source_columns=("rule_version",)),
    CleanupSpec("screening_result_items", id_columns=("screening_item_id", "screening_id", "universe_id", "asset_id")),
    CleanupSpec("screening_results", id_columns=("screening_id", "universe_id"), source_columns=("strategy",)),
    CleanupSpec("signal_snapshots", id_columns=("signal_id", "asset_id"), source_columns=("rule_version",)),
    CleanupSpec("risk_findings", id_columns=("risk_id", "asset_id")),
    CleanupSpec("factor_frames", id_columns=("factor_frame_id", "asset_id", "indicator_frame_id")),
    CleanupSpec("indicator_frames", id_columns=("indicator_frame_id", "asset_id")),
    CleanupSpec("data_quality_snapshots", id_columns=("quality_id", "asset_id")),
    CleanupSpec("fundamental_snapshots", id_columns=("snapshot_id", "asset_id"), source_columns=("source",)),
    CleanupSpec("capital_flow_snapshots", id_columns=("snapshot_id", "asset_id"), source_columns=("source",)),
    CleanupSpec("event_records", id_columns=("event_id", "asset_id"), source_columns=("source",)),
    CleanupSpec("crypto_derivative_snapshots", id_columns=("snapshot_id", "asset_id"), source_columns=("source",)),
    CleanupSpec("market_bars", id_columns=("asset_id", "raw_record_id"), source_columns=("source",), payload_source=False),
    CleanupSpec("raw_records", id_columns=("raw_record_id", "asset_id"), source_columns=("provider", "endpoint"), payload_source=False),
    CleanupSpec("evidence", id_columns=("evidence_id", "asset_id"), source_columns=("source",)),
    CleanupSpec("asset_universe_members", id_columns=("universe_id", "asset_id")),
    CleanupSpec("asset_universes", id_columns=("universe_id", "owner_id", "base_universe_id"), source_columns=("source", "strategy_context")),
    CleanupSpec("model_routing_rules", id_columns=("rule_id", "model_key"), source_columns=("workflow_type", "decision_type")),
    CleanupSpec("retrieval_profiles", id_columns=("profile_id", "profile_key", "embedding_model_key", "rerank_model_key")),
    CleanupSpec("model_instances", id_columns=("model_instance_id", "provider_key", "model_key")),
    CleanupSpec("model_providers", id_columns=("provider_id", "provider_key")),
    CleanupSpec("portfolios", id_columns=("portfolio_id", "owner_id")),
    CleanupSpec("watchlists", id_columns=("watchlist_id", "owner_id"), source_columns=("purpose",)),
    CleanupSpec(
        "assets",
        id_columns=("asset_id", "symbol", "name"),
        payload_source=False,
        note="资产主表只清理测试资产标识，不按 payload.source 删除真实资产。",
    ),
)


def build_where_clause(spec: CleanupSpec, *, existing_columns: set[str]) -> str:
    """根据表规则和实际列构造保守的 smoke 匹配条件。"""

    clauses: list[str] = []
    for column in spec.id_columns:
        if column in existing_columns:
            clauses.append(f"coalesce({quote_identifier(column)}::text, '') ilike '%smoke%'")
    for column in spec.source_columns:
        if column in existing_columns:
            clauses.append(f"coalesce({quote_identifier(column)}::text, '') ilike '%smoke%'")
    if spec.payload_source and "payload" in existing_columns:
        clauses.extend(
            [
                "coalesce(payload->>'source', '') ilike '%smoke%'",
                "coalesce(payload->>'purpose', '') ilike '%smoke%'",
            ]
        )
    if spec.payload_text and "payload" in existing_columns:
        clauses.append("coalesce(payload::text, '') ilike '%smoke%'")
    clauses.extend(spec.extra_conditions)
    if not clauses:
        return "false"
    return " or ".join(f"({clause})" for clause in clauses)


def build_delete_plans(engine: Engine, specs: tuple[CleanupSpec, ...] = DEFAULT_CLEANUP_SPECS) -> list[DeletePlan]:
    """根据数据库当前表结构生成删除计划。"""

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    plans: list[DeletePlan] = []
    for spec in specs:
        if spec.disabled or spec.table not in table_names:
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(spec.table)}
        where_clause = build_where_clause(spec, existing_columns=existing_columns)
        table_name = quote_identifier(spec.table)
        plans.append(
            DeletePlan(
                spec=spec,
                where_clause=where_clause,
                query_sql=f"select * from {table_name} where {where_clause}",
                delete_sql=f"delete from {table_name} where {where_clause}",
            )
        )
    return plans


def run_smoke_cleanup(
    engine: Engine,
    *,
    backup_root: Path = DEFAULT_BACKUP_ROOT,
    execute: bool = False,
    specs: tuple[CleanupSpec, ...] = DEFAULT_CLEANUP_SPECS,
) -> CleanupResult:
    """备份并清理 smoke 测试数据。

    execute=False 时只生成预览备份，不删除数据。
    """

    started_at = datetime.now(tz=UTC)
    backup_dir = backup_root / f"smoke_cleanup_{started_at.strftime('%Y%m%d_%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    plans = build_delete_plans(engine, specs)
    table_results: list[CleanupTableResult] = []
    with engine.begin() as connection:
        for plan in plans:
            rows = [dict(row) for row in connection.execute(text(plan.query_sql)).mappings()]
            backup_file = None
            if rows:
                backup_path = backup_dir / f"{plan.spec.table}.jsonl"
                write_jsonl_backup(backup_path, rows)
                backup_file = str(backup_path)
            deleted = 0
            if execute and rows:
                result = connection.execute(text(plan.delete_sql))
                deleted = int(result.rowcount or 0)
            table_results.append(
                CleanupTableResult(
                    table=plan.spec.table,
                    matched=len(rows),
                    deleted=deleted,
                    backup_file=backup_file,
                )
            )

    finished_at = datetime.now(tz=UTC)
    cleanup_result = CleanupResult(
        backup_dir=str(backup_dir),
        dry_run=not execute,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        tables=tuple(table_results),
    )
    write_manifest(backup_dir / "manifest.json", cleanup_result, plans)
    return cleanup_result


def write_jsonl_backup(path: Path, rows: list[JsonDict]) -> None:
    """写入单表 JSONL 备份。"""

    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, default=json_default))
            file.write("\n")


def write_manifest(path: Path, result: CleanupResult, plans: list[DeletePlan]) -> None:
    """写入清理 manifest，记录规则和结果。"""

    plan_by_table = {plan.spec.table: plan for plan in plans}
    payload = {
        "backup_dir": result.backup_dir,
        "dry_run": result.dry_run,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "matched_total": result.matched_total,
        "deleted_total": result.deleted_total,
        "tables": [
            {
                "table": table.table,
                "matched": table.matched,
                "deleted": table.deleted,
                "backup_file": table.backup_file,
                "where": plan_by_table[table.table].where_clause,
            }
            for table in result.tables
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def quote_identifier(identifier: str) -> str:
    """安全引用 SQL 标识符。"""

    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def json_default(value: Any) -> Any:
    """JSON 备份序列化兜底。"""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)
