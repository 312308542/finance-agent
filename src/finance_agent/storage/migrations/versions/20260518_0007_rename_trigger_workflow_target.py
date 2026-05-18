"""修正触发事件目标字段为 Agent 唤醒语义。

Revision ID: 20260518_0007
Revises: 20260518_0006
Create Date: 2026-05-18 14:10:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260518_0007"
down_revision = "20260518_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_trigger_events_workflow", table_name="assistant_trigger_events")
    op.alter_column(
        "assistant_trigger_events",
        "workflow_type",
        new_column_name="requested_workflow_type",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "assistant_trigger_events",
        "workflow_run_id",
        new_column_name="agent_task_id",
        existing_type=sa.String(length=160),
        existing_nullable=True,
    )
    op.add_column(
        "assistant_trigger_events",
        sa.Column(
            "agent_runtime",
            sa.String(length=64),
            server_default=sa.text("'hermes_agent'"),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_trigger_events_agent_runtime",
        "assistant_trigger_events",
        ["agent_runtime", "status"],
    )
    op.create_index(
        "idx_trigger_events_requested_workflow",
        "assistant_trigger_events",
        ["requested_workflow_type", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_trigger_events_requested_workflow", table_name="assistant_trigger_events")
    op.drop_index("idx_trigger_events_agent_runtime", table_name="assistant_trigger_events")
    op.drop_column("assistant_trigger_events", "agent_runtime")
    op.alter_column(
        "assistant_trigger_events",
        "agent_task_id",
        new_column_name="workflow_run_id",
        existing_type=sa.String(length=160),
        existing_nullable=True,
    )
    op.alter_column(
        "assistant_trigger_events",
        "requested_workflow_type",
        new_column_name="workflow_type",
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_index(
        "idx_trigger_events_workflow",
        "assistant_trigger_events",
        ["workflow_type", "status"],
    )
