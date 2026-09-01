"""扩展持久调度任务状态、准入和配置身份字段。

Revision ID: 20260831_0027
Revises: 20260823_0026
Create Date: 2026-08-31 18:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260831_0027"
down_revision = "20260823_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增量扩展已有任务表，保留历史任务。"""

    op.drop_constraint("ck_scheduler_task_runs_status", "scheduler_task_runs", type_="check")
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "schedule_type", sa.String(length=32), server_default="manual", nullable=False
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "scheduled_for", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "priority", sa.Integer(), server_default="100", nullable=False
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "resource_pool", sa.String(length=64), server_default="default", nullable=False
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "mutex_key", sa.String(length=160), nullable=True
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "dependency_generation",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "required_data_domains",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "blocked_reason", sa.String(length=64), nullable=True
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "blocked_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "blocked_until", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "config_digest", sa.String(length=64), nullable=True
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "coalesced_count", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "scheduler_task_runs",
        sa.Column(
            "cancel_requested_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_check_constraint(
        "ck_scheduler_task_runs_status",
        "scheduler_task_runs",
        "status IN ('scheduled', 'blocked', 'pending', 'running', "
        "'completed', 'failed', 'cancelled')",
    )
    op.create_index(
        "idx_scheduler_task_runs_scheduled",
        "scheduler_task_runs",
        ["status", "scheduled_for", "priority"],
    )
    op.create_index(
        "idx_scheduler_task_runs_pool",
        "scheduler_task_runs",
        ["status", "resource_pool"],
    )
    op.create_index(
        "idx_scheduler_task_runs_mutex",
        "scheduler_task_runs",
        ["status", "mutex_key"],
    )


def downgrade() -> None:
    """移除新增字段并恢复旧状态约束，不删除任务表。"""

    op.drop_index("idx_scheduler_task_runs_mutex", table_name="scheduler_task_runs")
    op.drop_index("idx_scheduler_task_runs_pool", table_name="scheduler_task_runs")
    op.drop_index("idx_scheduler_task_runs_scheduled", table_name="scheduler_task_runs")
    op.execute(
        "UPDATE scheduler_task_runs SET status = 'pending' "
        "WHERE status IN ('scheduled', 'blocked')"
    )
    op.drop_constraint("ck_scheduler_task_runs_status", "scheduler_task_runs", type_="check")
    op.create_check_constraint(
        "ck_scheduler_task_runs_status",
        "scheduler_task_runs",
        "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
    )
    op.drop_column("scheduler_task_runs", "cancel_requested_at")
    op.drop_column("scheduler_task_runs", "coalesced_count")
    op.drop_column("scheduler_task_runs", "config_digest")
    op.drop_column("scheduler_task_runs", "blocked_until")
    op.drop_column("scheduler_task_runs", "blocked_detail")
    op.drop_column("scheduler_task_runs", "blocked_reason")
    op.drop_column("scheduler_task_runs", "required_data_domains")
    op.drop_column("scheduler_task_runs", "dependency_generation")
    op.drop_column("scheduler_task_runs", "mutex_key")
    op.drop_column("scheduler_task_runs", "resource_pool")
    op.drop_column("scheduler_task_runs", "priority")
    op.drop_column("scheduler_task_runs", "scheduled_for")
    op.drop_column("scheduler_task_runs", "schedule_type")
