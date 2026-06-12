"""创建评分策略配置表。

Revision ID: 20260612_0017
Revises: 20260606_0016
Create Date: 2026-06-12 16:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from finance_agent.scoring.strategies import default_scoring_strategy_seeds

revision = "20260612_0017"
down_revision = "20260606_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scoring_strategies",
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("group_weights", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "missing_penalty",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'draft'"),
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
        sa.PrimaryKeyConstraint("strategy_id"),
    )
    op.create_index(
        "idx_scoring_strategies_market_status",
        "scoring_strategies",
        ["market", "status"],
    )
    op.create_index("idx_scoring_strategies_status", "scoring_strategies", ["status"])
    op.execute(
        """
        COMMENT ON TABLE scoring_strategies IS
        '评分策略配置表，保存不同推荐策略的因子组权重和缺失惩罚参数。';
        COMMENT ON COLUMN scoring_strategies.strategy_id IS '评分策略 ID，例如 strategy:ashare:short_swing。';
        COMMENT ON COLUMN scoring_strategies.market IS '适用市场，例如 ashare、crypto_spot、crypto_future。';
        COMMENT ON COLUMN scoring_strategies.name IS '策略中文名称。';
        COMMENT ON COLUMN scoring_strategies.description IS '策略说明。';
        COMMENT ON COLUMN scoring_strategies.group_weights IS '因子组权重 JSON，权重和必须接近 1。';
        COMMENT ON COLUMN scoring_strategies.missing_penalty IS '缺失因子组和部分缺失因子组的扣分配置。';
        COMMENT ON COLUMN scoring_strategies.status IS '策略状态：active、draft、archived。';
        COMMENT ON COLUMN scoring_strategies.created_at IS '创建时间。';
        COMMENT ON COLUMN scoring_strategies.updated_at IS '最近更新时间。';
        """
    )
    op.bulk_insert(
        sa.table(
            "scoring_strategies",
            sa.column("strategy_id", sa.String),
            sa.column("market", sa.String),
            sa.column("name", sa.String),
            sa.column("description", sa.Text),
            sa.column("group_weights", postgresql.JSONB),
            sa.column("missing_penalty", postgresql.JSONB),
            sa.column("status", sa.String),
        ),
        default_scoring_strategy_seeds(),
    )


def downgrade() -> None:
    op.drop_index("idx_scoring_strategies_status", table_name="scoring_strategies")
    op.drop_index("idx_scoring_strategies_market_status", table_name="scoring_strategies")
    op.drop_table("scoring_strategies")
