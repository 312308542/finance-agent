"""创建用户投资画像表。

Revision ID: 20260630_0020
Revises: 20260614_0019
Create Date: 2026-06-30 03:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260630_0020"
down_revision = "20260614_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_investment_profiles",
        sa.Column("profile_id", sa.String(length=160), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("risk_appetite", sa.String(length=32), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("capital_scale", sa.String(length=64), nullable=False),
        sa.Column(
            "style_tendency",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("timing_posture", sa.String(length=32), nullable=False),
        sa.Column(
            "dimension_confidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
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
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("profile_id"),
    )
    op.create_index(
        "idx_user_investment_profiles_owner_status",
        "user_investment_profiles",
        ["owner_id", "status"],
    )
    op.create_index(
        "idx_user_investment_profiles_updated",
        "user_investment_profiles",
        ["updated_at"],
    )

    op.execute(
        """
        COMMENT ON TABLE user_investment_profiles IS
        '用户投资画像表，保存风险偏好、投资周期、风格倾向、择时姿态及各维度置信度。';
        COMMENT ON COLUMN user_investment_profiles.profile_id IS '画像 ID，格式为 profile:{owner_id}。';
        COMMENT ON COLUMN user_investment_profiles.owner_id IS '画像归属用户或主体 ID。';
        COMMENT ON COLUMN user_investment_profiles.risk_appetite IS '风险偏好：conservative、balanced、aggressive。';
        COMMENT ON COLUMN user_investment_profiles.horizon IS '投资周期：swing、mid_long、mixed。';
        COMMENT ON COLUMN user_investment_profiles.capital_scale IS '可投金额量级，保存区间或 unknown，不保存精确敏感金额。';
        COMMENT ON COLUMN user_investment_profiles.style_tendency IS '风格倾向 JSON，例如 value/theme 权重。';
        COMMENT ON COLUMN user_investment_profiles.timing_posture IS '择时姿态：defensive、neutral、opportunistic。';
        COMMENT ON COLUMN user_investment_profiles.dimension_confidence IS '各画像维度置信度 JSON，取值范围 0~1。';
        COMMENT ON COLUMN user_investment_profiles.source IS '各画像维度来源 JSON，例如 default、elicited、inferred。';
        COMMENT ON COLUMN user_investment_profiles.status IS '画像状态：active 或 stale。';
        COMMENT ON COLUMN user_investment_profiles.created_at IS '画像创建时间。';
        COMMENT ON COLUMN user_investment_profiles.updated_at IS '画像最近更新时间。';
        COMMENT ON COLUMN user_investment_profiles.payload IS '扩展审计信息 JSON，例如证据链、推断原因和版本。';
        """
    )


def downgrade() -> None:
    op.drop_index("idx_user_investment_profiles_updated", table_name="user_investment_profiles")
    op.drop_index(
        "idx_user_investment_profiles_owner_status",
        table_name="user_investment_profiles",
    )
    op.drop_table("user_investment_profiles")
