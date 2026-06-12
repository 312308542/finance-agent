"""创建轻量回测结果表。

Revision ID: 20260614_0019
Revises: 20260613_0018
Create Date: 2026-06-14 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260614_0019"
down_revision = "20260613_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtest_results",
        sa.Column("backtest_id", sa.String(length=192), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("universe_id", sa.String(length=128), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rebalance_frequency", sa.String(length=32), nullable=False),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "data_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
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
        sa.PrimaryKeyConstraint("backtest_id"),
        sa.CheckConstraint("start_at < end_at", name="ck_backtest_results_time_window"),
        sa.CheckConstraint(
            "length(btrim(rebalance_frequency)) > 0",
            name="ck_backtest_results_rebalance_nonempty",
        ),
    )
    op.create_index(
        "idx_backtest_results_strategy_created",
        "backtest_results",
        ["strategy_id", "created_at"],
    )
    op.create_index(
        "idx_backtest_results_universe_created",
        "backtest_results",
        ["universe_id", "created_at"],
    )
    op.create_index(
        "idx_backtest_results_market_status",
        "backtest_results",
        ["market", "status"],
    )
    op.create_index(
        "idx_backtest_results_window",
        "backtest_results",
        ["start_at", "end_at"],
    )

    op.execute(
        """
        COMMENT ON TABLE backtest_results IS
        '回测结果表，保存轻量策略历史验证摘要、绩效指标和可复现数据版本。';
        COMMENT ON COLUMN backtest_results.backtest_id IS '回测结果 ID，例如 backtest:strategy:market:timestamp。';
        COMMENT ON COLUMN backtest_results.market IS '市场标识，例如 ashare、fund、crypto_spot。';
        COMMENT ON COLUMN backtest_results.strategy_id IS '评分或推荐策略 ID。';
        COMMENT ON COLUMN backtest_results.universe_id IS '候选池 ID，标识本次回测使用的资产范围。';
        COMMENT ON COLUMN backtest_results.start_at IS '回测窗口开始时间。';
        COMMENT ON COLUMN backtest_results.end_at IS '回测窗口结束时间。';
        COMMENT ON COLUMN backtest_results.rebalance_frequency IS '再平衡频率，例如 once、weekly、monthly。';
        COMMENT ON COLUMN backtest_results.metrics IS '核心绩效指标 JSON，例如 CAGR、最大回撤、夏普、Sortino 和胜率口径。';
        COMMENT ON COLUMN backtest_results.data_versions IS '数据版本 JSON，记录 K 线水位、评分模式、规则版本等复现信息。';
        COMMENT ON COLUMN backtest_results.status IS '回测结果状态，例如 available、partial、failed。';
        COMMENT ON COLUMN backtest_results.created_at IS '结果入库时间。';
        COMMENT ON COLUMN backtest_results.payload IS '扩展明细 JSON，例如月度收益、回撤序列、净值曲线和警告信息。';
        """
    )


def downgrade() -> None:
    op.drop_index("idx_backtest_results_window", table_name="backtest_results")
    op.drop_index("idx_backtest_results_market_status", table_name="backtest_results")
    op.drop_index("idx_backtest_results_universe_created", table_name="backtest_results")
    op.drop_index("idx_backtest_results_strategy_created", table_name="backtest_results")
    op.drop_table("backtest_results")
