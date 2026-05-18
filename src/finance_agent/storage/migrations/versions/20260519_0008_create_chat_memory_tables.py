"""创建 CLI 聊天记忆表。

Revision ID: 20260519_0008
Revises: 20260518_0007
Create Date: 2026-05-19 10:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260519_0008"
down_revision = "20260518_0007"
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
        "assistant_chat_sessions",
        sa.Column("chat_session_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("chat_session_id"),
    )
    op.create_index(
        "idx_chat_sessions_owner_status",
        "assistant_chat_sessions",
        ["owner_id", "status"],
    )
    op.create_index(
        "idx_chat_sessions_owner_updated",
        "assistant_chat_sessions",
        ["owner_id", "updated_at"],
    )
    op.create_index(
        "idx_chat_sessions_last_message",
        "assistant_chat_sessions",
        ["owner_id", "last_message_at"],
    )

    op.create_table(
        "assistant_chat_messages",
        sa.Column("chat_message_id", sa.String(length=192), nullable=False),
        sa.Column("chat_session_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=True),
        _jsonb_object("data"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("chat_message_id"),
        sa.UniqueConstraint(
            "chat_session_id",
            "sequence_no",
            name="uq_chat_messages_session_seq",
        ),
    )
    op.create_index(
        "idx_chat_messages_session_seq",
        "assistant_chat_messages",
        ["chat_session_id", "sequence_no"],
    )
    op.create_index(
        "idx_chat_messages_owner_created",
        "assistant_chat_messages",
        ["owner_id", "created_at"],
    )
    op.create_index(
        "idx_chat_messages_intent",
        "assistant_chat_messages",
        ["intent"],
    )


def downgrade() -> None:
    op.drop_table("assistant_chat_messages")
    op.drop_table("assistant_chat_sessions")
