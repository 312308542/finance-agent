"""创建业务事件 Outbox 表。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260720_0024"
down_revision = "20260720_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_type", sa.String(length=128), nullable=False),
        sa.Column("aggregate_id", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_stream_id", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("publish_lease_owner", sa.String(length=128), nullable=True),
        sa.Column("publish_lease_token", sa.String(length=128), nullable=True),
        sa.Column("publish_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_events_idempotency"),
    )
    op.create_index(
        "idx_outbox_events_pending",
        "outbox_events",
        ["published_at", "available_at", "created_at"],
    )
    op.create_index(
        "idx_outbox_events_lease", "outbox_events", ["publish_lease_expires_at"]
    )
    op.create_index(
        "idx_outbox_events_type_created", "outbox_events", ["event_type", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("idx_outbox_events_type_created", table_name="outbox_events")
    op.drop_index("idx_outbox_events_lease", table_name="outbox_events")
    op.drop_index("idx_outbox_events_pending", table_name="outbox_events")
    op.drop_table("outbox_events")
