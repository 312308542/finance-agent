"""创建私人金融助手触发事件表

Revision ID: 20260518_0006
Revises: 20260517_0005
Create Date: 2026-05-18 12:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260518_0006"
down_revision = "20260517_0005"
branch_labels = None
depends_on = None


def _jsonb_object(name: str = "payload") -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "assistant_trigger_events",
        sa.Column("trigger_event_id", sa.String(length=192), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("trigger_type", sa.String(length=64), nullable=False),
        sa.Column("trigger_ref", sa.String(length=192), nullable=True),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("workflow_type", sa.String(length=64), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=160), nullable=True),
        sa.Column("portfolio_id", sa.String(length=128), nullable=True),
        sa.Column("watchlist_id", sa.String(length=128), nullable=True),
        sa.Column("recommendation_run_id", sa.String(length=160), nullable=True),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("trigger_event_id"),
        sa.UniqueConstraint("dedup_key", "triggered_at", name="uq_trigger_events_dedup_time"),
    )
    op.create_index(
        "idx_trigger_events_owner_status",
        "assistant_trigger_events",
        ["owner_id", "status"],
    )
    op.create_index(
        "idx_trigger_events_type_status",
        "assistant_trigger_events",
        ["trigger_type", "status"],
    )
    op.create_index(
        "idx_trigger_events_asset_time",
        "assistant_trigger_events",
        ["asset_id", "triggered_at"],
    )
    op.create_index(
        "idx_trigger_events_dedup_time",
        "assistant_trigger_events",
        ["dedup_key", "triggered_at"],
    )
    op.create_index(
        "idx_trigger_events_workflow",
        "assistant_trigger_events",
        ["workflow_type", "status"],
    )


def downgrade() -> None:
    op.drop_table("assistant_trigger_events")
