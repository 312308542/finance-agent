"""创建停跑恢复补跑批次、步骤和缺口目标区间表。

Revision ID: 20260823_0026
Revises: 20260720_0025
Create Date: 2026-08-23 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260823_0026"
down_revision = "20260720_0025"
branch_labels = None
depends_on = None

_ACTIVE_RUN_STATUSES = (
    "'draft', 'approved', 'running', 'paused', 'verifying', 'attention_required'"
)


def _jsonb_object(name: str) -> sa.Column:
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


def _timestamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "data_recovery_runs",
        sa.Column("run_id", sa.String(length=192), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("universe_id", sa.String(length=160), nullable=True),
        sa.Column("universe_snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("universe_snapshot_hash", sa.String(length=128), nullable=True),
        sa.Column("gap_start_date", sa.Date(), nullable=True),
        sa.Column("cutoff_date", sa.Date(), nullable=False),
        sa.Column("plan_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column(
            "gate_status",
            sa.String(length=32),
            server_default=sa.text("'degraded'"),
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(length=128), nullable=True),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        _jsonb_object("summary"),
        _jsonb_object("quality_result"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _timestamp("approved_at"),
        _timestamp("started_at"),
        _timestamp("finished_at"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'running', 'paused', 'verifying', "
            "'attention_required', 'completed', 'completed_with_exceptions', 'cancelled')",
            name="ck_data_recovery_runs_status",
        ),
        sa.CheckConstraint(
            "gate_status IN ('recovering', 'degraded', 'open')",
            name="ck_data_recovery_runs_gate_status",
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    # 同一市场同一时间只允许一个草稿或活动批次（互斥范围 data_recovery:{market}）。
    op.create_index(
        "uq_data_recovery_runs_active_market",
        "data_recovery_runs",
        ["market"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({_ACTIVE_RUN_STATUSES})"),
    )
    op.create_index(
        "idx_data_recovery_runs_market_created",
        "data_recovery_runs",
        ["market", "created_at"],
    )
    op.create_index("idx_data_recovery_runs_status", "data_recovery_runs", ["status"])
    op.create_index(
        "idx_data_recovery_runs_plan_hash", "data_recovery_runs", ["plan_hash"]
    )

    op.create_table(
        "data_recovery_steps",
        sa.Column("step_id", sa.String(length=224), nullable=False),
        sa.Column("run_id", sa.String(length=192), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("data_domain", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False
        ),
        _jsonb_array("depends_on"),
        sa.Column("target_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "completed_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "retryable_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "exception_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        _jsonb_object("task_params"),
        sa.Column(
            "attempt_round", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        _timestamp("started_at"),
        _timestamp("finished_at"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'cancelled')",
            name="ck_data_recovery_steps_status",
        ),
        sa.PrimaryKeyConstraint("step_id"),
        sa.UniqueConstraint(
            "run_id", "phase", "data_domain", name="uq_data_recovery_steps_scope"
        ),
    )
    op.create_index(
        "idx_data_recovery_steps_run_status",
        "data_recovery_steps",
        ["run_id", "status"],
    )

    op.create_table(
        "data_recovery_targets",
        sa.Column("target_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=192), nullable=False),
        sa.Column("step_id", sa.String(length=224), nullable=False),
        sa.Column("data_domain", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("gap_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gap_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "granularity", sa.String(length=32), server_default=sa.text("'1d'"), nullable=False
        ),
        sa.Column(
            "expected_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("exception_code", sa.String(length=32), nullable=True),
        _jsonb_object("exception_evidence"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'exception', 'excluded')",
            name="ck_data_recovery_targets_status",
        ),
        sa.PrimaryKeyConstraint("target_id"),
    )
    # 市场级目标 asset_id 允许为空，唯一性用 coalesce 归一后判定，覆盖批次+域+资产+区间+粒度。
    op.create_index(
        "uq_data_recovery_targets_scope",
        "data_recovery_targets",
        [
            "run_id",
            "data_domain",
            sa.text("coalesce(asset_id, '')"),
            "gap_start_at",
            "gap_end_at",
            "granularity",
        ],
        unique=True,
    )
    op.create_index(
        "idx_data_recovery_targets_run_status",
        "data_recovery_targets",
        ["run_id", "status"],
    )
    op.create_index(
        "idx_data_recovery_targets_step_status",
        "data_recovery_targets",
        ["step_id", "status"],
    )
    op.create_index(
        "idx_data_recovery_targets_retry",
        "data_recovery_targets",
        ["run_id", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_data_recovery_targets_retry", table_name="data_recovery_targets"
    )
    op.drop_index(
        "idx_data_recovery_targets_step_status", table_name="data_recovery_targets"
    )
    op.drop_index(
        "idx_data_recovery_targets_run_status", table_name="data_recovery_targets"
    )
    op.drop_index("uq_data_recovery_targets_scope", table_name="data_recovery_targets")
    op.drop_table("data_recovery_targets")

    op.drop_index(
        "idx_data_recovery_steps_run_status", table_name="data_recovery_steps"
    )
    op.drop_table("data_recovery_steps")

    op.drop_index("idx_data_recovery_runs_plan_hash", table_name="data_recovery_runs")
    op.drop_index("idx_data_recovery_runs_status", table_name="data_recovery_runs")
    op.drop_index(
        "idx_data_recovery_runs_market_created", table_name="data_recovery_runs"
    )
    op.drop_index(
        "uq_data_recovery_runs_active_market", table_name="data_recovery_runs"
    )
    op.drop_table("data_recovery_runs")
