"""创建不可变数据快照、决策闸门和持久化任务表。

Revision ID: 20260720_0023
Revises: 20260716_0022
Create Date: 2026-07-20 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260720_0023"
down_revision = "20260716_0022"
branch_labels = None
depends_on = None


def _jsonb_array(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    )


def _jsonb_object(name: str = "payload") -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "data_snapshots",
        sa.Column("data_snapshot_id", sa.String(length=255), nullable=False),
        sa.Column("snapshot_type", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("provider_version", sa.String(length=128), nullable=True),
        sa.Column("quality_status", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        _jsonb_array("raw_record_ids"),
        _jsonb_object("payload"),
        _jsonb_object("metadata"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quality_status IN ('available', 'partial', 'stale', 'conflict', 'unavailable', "
            "'invalid_server_time', 'after_hours_snapshot', 'clock_skew')",
            name="ck_data_snapshots_quality_status",
        ),
        sa.PrimaryKeyConstraint("data_snapshot_id"),
    )
    op.create_index(
        "idx_data_snapshots_type_asof", "data_snapshots", ["snapshot_type", "as_of"]
    )
    op.create_index(
        "idx_data_snapshots_provider_captured",
        "data_snapshots",
        ["provider", "captured_at"],
    )
    op.create_index("idx_data_snapshots_quality", "data_snapshots", ["quality_status"])
    op.create_index("idx_data_snapshots_content_hash", "data_snapshots", ["content_hash"])

    op.create_table(
        "decision_gates",
        sa.Column("decision_gate_id", sa.String(length=255), nullable=False),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("data_snapshot_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        _jsonb_array("reason_codes"),
        _jsonb_array("reasons"),
        _jsonb_array("evidence_ids"),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.String(length=128), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object("payload"),
        sa.CheckConstraint(
            "status IN ('approved', 'rejected', 'pending_review', 'data_unavailable', 'expired')",
            name="ck_decision_gates_status",
        ),
        sa.PrimaryKeyConstraint("decision_gate_id"),
    )
    op.create_index("idx_decision_gates_snapshot", "decision_gates", ["data_snapshot_id"])
    op.create_index(
        "idx_decision_gates_status_evaluated",
        "decision_gates",
        ["status", "evaluated_at"],
    )
    op.create_index(
        "idx_decision_gates_type_evaluated",
        "decision_gates",
        ["decision_type", "evaluated_at"],
    )

    op.create_table(
        "scheduler_task_runs",
        sa.Column("task_id", sa.String(length=192), nullable=False),
        sa.Column("job_name", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False
        ),
        _jsonb_object("payload"),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_scheduler_task_runs_status",
        ),
        sa.PrimaryKeyConstraint("task_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_scheduler_task_runs_idempotency"),
    )
    op.create_index(
        "idx_scheduler_task_runs_due",
        "scheduler_task_runs",
        ["status", "next_retry_at", "created_at"],
    )
    op.create_index(
        "idx_scheduler_task_runs_lease",
        "scheduler_task_runs",
        ["status", "lease_expires_at"],
    )
    op.create_index(
        "idx_scheduler_task_runs_job_created",
        "scheduler_task_runs",
        ["job_name", "created_at"],
    )

    op.add_column(
        "realtime_quote_snapshots",
        sa.Column("data_snapshot_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "idx_realtime_quotes_snapshot", "realtime_quote_snapshots", ["data_snapshot_id"]
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_data_snapshot_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'data_snapshots is append-only: % is not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_data_snapshots_append_only
        BEFORE UPDATE OR DELETE ON data_snapshots
        FOR EACH ROW EXECUTE FUNCTION prevent_data_snapshot_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_data_snapshots_append_only ON data_snapshots")
    op.execute("DROP FUNCTION IF EXISTS prevent_data_snapshot_mutation()")
    op.drop_index("idx_realtime_quotes_snapshot", table_name="realtime_quote_snapshots")
    op.drop_column("realtime_quote_snapshots", "data_snapshot_id")
    op.drop_table("scheduler_task_runs")
    op.drop_table("decision_gates")
    op.drop_table("data_snapshots")
