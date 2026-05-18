"""创建私人金融助手 M2 表

Revision ID: 20260517_0004
Revises: 20260515_0003
Create Date: 2026-05-17 00:40:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260517_0004"
down_revision = "20260515_0003"
branch_labels = None
depends_on = None


def _jsonb_object(name: str = "payload") -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def _jsonb_array(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "portfolios",
        sa.Column("portfolio_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("portfolio_type", sa.String(length=32), nullable=False),
        sa.Column("base_currency", sa.String(length=16), nullable=False),
        sa.Column("risk_profile", sa.String(length=64), nullable=False),
        sa.Column("total_equity", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("cash", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("market_value", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("max_position_weight", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("max_drawdown_alert", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("portfolio_id"),
    )
    op.create_index("idx_portfolios_owner", "portfolios", ["owner_id"])
    op.create_index("idx_portfolios_status", "portfolios", ["status"])

    op.create_table(
        "positions",
        sa.Column("position_id", sa.String(length=160), nullable=False),
        sa.Column("portfolio_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=36, scale=10), nullable=False),
        sa.Column("avg_cost", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("last_price", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("market_value", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("unrealized_pnl_pct", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("portfolio_weight", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("leverage", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("liquidation_price", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("position_id"),
        sa.UniqueConstraint(
            "portfolio_id",
            "asset_id",
            "side",
            name="uq_positions_portfolio_asset_side",
        ),
    )
    op.create_index("idx_positions_portfolio", "positions", ["portfolio_id"])
    op.create_index("idx_positions_asset", "positions", ["asset_id"])
    op.create_index("idx_positions_market", "positions", ["market"])

    op.create_table(
        "watchlists",
        sa.Column("watchlist_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=True),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("watchlist_id"),
    )
    op.create_index("idx_watchlists_owner_status", "watchlists", ["owner_id", "status"])
    op.create_index("idx_watchlists_market", "watchlists", ["market"])

    op.create_table(
        "watchlist_items",
        sa.Column("watchlist_item_id", sa.String(length=160), nullable=False),
        sa.Column("watchlist_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=192), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        _jsonb_object("watch_conditions"),
        _jsonb_object("trigger_conditions"),
        _jsonb_object("invalid_conditions"),
        sa.Column("risk_level", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_reason", sa.Text(), nullable=True),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("watchlist_item_id"),
        sa.UniqueConstraint(
            "watchlist_id",
            "asset_id",
            name="uq_watchlist_items_watchlist_asset",
        ),
    )
    op.create_index(
        "idx_watchlist_items_watchlist_status",
        "watchlist_items",
        ["watchlist_id", "status"],
    )
    op.create_index("idx_watchlist_items_asset", "watchlist_items", ["asset_id"])
    op.create_index("idx_watchlist_items_next_review", "watchlist_items", ["next_review_at"])

    op.create_table(
        "asset_theses",
        sa.Column("thesis_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=192), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=False),
        _jsonb_array("supporting_points"),
        _jsonb_array("risk_points"),
        _jsonb_object("invalid_if"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("thesis_id"),
    )
    op.create_index("idx_asset_theses_asset_status", "asset_theses", ["asset_id", "status"])
    op.create_index("idx_asset_theses_source", "asset_theses", ["source_type", "source_id"])

    op.create_table(
        "monitoring_alerts",
        sa.Column("alert_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=128), nullable=True),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("alert_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("triggered_by", sa.String(length=64), nullable=False),
        sa.Column("trigger_condition", sa.Text(), nullable=False),
        sa.Column("current_value", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("threshold_value", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("alert_id"),
    )
    op.create_index("idx_alerts_owner_status", "monitoring_alerts", ["owner_id", "status"])
    op.create_index("idx_alerts_asset_asof", "monitoring_alerts", ["asset_id", "as_of"])
    op.create_index("idx_alerts_severity", "monitoring_alerts", ["severity"])

    op.create_table(
        "decision_logs",
        sa.Column("decision_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=128), nullable=True),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column("source_recommendation_id", sa.String(length=192), nullable=True),
        sa.Column("source_alert_id", sa.String(length=160), nullable=True),
        sa.Column("workflow_run_id", sa.String(length=160), nullable=True),
        sa.Column("suggested_action", sa.String(length=64), nullable=False),
        sa.Column("user_action", sa.String(length=64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        _jsonb_array("reason_ids"),
        _jsonb_array("risk_ids"),
        _jsonb_array("evidence_ids"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("decision_id"),
    )
    op.create_index(
        "idx_decision_logs_owner_created",
        "decision_logs",
        ["owner_id", "created_at"],
    )
    op.create_index(
        "idx_decision_logs_asset_created",
        "decision_logs",
        ["asset_id", "created_at"],
    )
    op.create_index("idx_decision_logs_type", "decision_logs", ["decision_type"])

    op.create_table(
        "assistant_memories",
        sa.Column("memory_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("source_decision_id", sa.String(length=160), nullable=True),
        sa.Column("source_review_task_id", sa.String(length=160), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding_ref", sa.String(length=160), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index("idx_memories_owner_type", "assistant_memories", ["owner_id", "memory_type"])
    op.create_index("idx_memories_scope_asset", "assistant_memories", ["scope", "asset_id"])
    op.create_index("idx_memories_status", "assistant_memories", ["status"])

    op.create_table(
        "memory_embeddings",
        sa.Column("embedding_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("memory_id", sa.String(length=160), nullable=True),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=192), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("embedding_id"),
    )
    op.create_index(
        "idx_memory_embeddings_owner_source",
        "memory_embeddings",
        ["owner_id", "source_type", "source_id"],
    )
    op.create_index("idx_memory_embeddings_memory", "memory_embeddings", ["memory_id"])
    op.create_index("idx_memory_embeddings_hash", "memory_embeddings", ["content_hash"])

    op.create_table(
        "financial_memory_edges",
        sa.Column("edge_id", sa.String(length=192), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=192), nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=192), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("edge_id"),
        sa.UniqueConstraint(
            "owner_id",
            "source_type",
            "source_id",
            "relation_type",
            "target_type",
            "target_id",
            name="uq_memory_edges_owner_source_relation_target",
        ),
    )
    op.create_index(
        "idx_memory_edges_source",
        "financial_memory_edges",
        ["owner_id", "source_type", "source_id"],
    )
    op.create_index(
        "idx_memory_edges_target",
        "financial_memory_edges",
        ["owner_id", "target_type", "target_id"],
    )
    op.create_index("idx_memory_edges_relation", "financial_memory_edges", ["relation_type"])

    op.create_table(
        "review_tasks",
        sa.Column("review_task_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("source_decision_id", sa.String(length=160), nullable=True),
        sa.Column("review_type", sa.String(length=64), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        _jsonb_array("review_questions"),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("review_task_id"),
    )
    op.create_index("idx_review_tasks_owner_due", "review_tasks", ["owner_id", "due_at"])
    op.create_index("idx_review_tasks_status", "review_tasks", ["status"])
    op.create_index("idx_review_tasks_source_decision", "review_tasks", ["source_decision_id"])

    op.create_table(
        "agent_workflow_runs",
        sa.Column("workflow_run_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("workflow_type", sa.String(length=64), nullable=False),
        sa.Column("trigger_type", sa.String(length=64), nullable=False),
        sa.Column("trigger_ref", sa.String(length=192), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("input_ref", sa.String(length=255), nullable=True),
        sa.Column("output_ref", sa.String(length=255), nullable=True),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("workflow_run_id"),
    )
    op.create_index(
        "idx_workflow_runs_owner_started",
        "agent_workflow_runs",
        ["owner_id", "started_at"],
    )
    op.create_index(
        "idx_workflow_runs_type_status",
        "agent_workflow_runs",
        ["workflow_type", "status"],
    )

    op.create_table(
        "agent_workflow_events",
        sa.Column("workflow_event_id", sa.String(length=192), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=64), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        _jsonb_array("evidence_ids"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("workflow_event_id"),
    )
    op.create_index(
        "idx_workflow_events_run_created",
        "agent_workflow_events",
        ["workflow_run_id", "created_at"],
    )
    op.create_index("idx_workflow_events_agent", "agent_workflow_events", ["agent_name"])


def downgrade() -> None:
    op.drop_table("agent_workflow_events")
    op.drop_table("agent_workflow_runs")
    op.drop_table("review_tasks")
    op.drop_table("financial_memory_edges")
    op.drop_table("memory_embeddings")
    op.drop_table("assistant_memories")
    op.drop_table("decision_logs")
    op.drop_table("monitoring_alerts")
    op.drop_table("asset_theses")
    op.drop_table("watchlist_items")
    op.drop_table("watchlists")
    op.drop_table("positions")
    op.drop_table("portfolios")
