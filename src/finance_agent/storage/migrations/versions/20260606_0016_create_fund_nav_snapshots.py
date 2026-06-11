"""创建开放式基金净值快照表。

Revision ID: 20260606_0016
Revises: 20260605_0015
Create Date: 2026-06-06 10:20:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260606_0016"
down_revision = "20260605_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_nav_snapshots",
        sa.Column("snapshot_id", sa.String(length=192), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("nav_date", sa.Date(), nullable=False),
        sa.Column("unit_nav", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("accumulated_nav", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("daily_return", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column("purchase_status", sa.String(length=64), nullable=True),
        sa.Column("redeem_status", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'available'"),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
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
        sa.PrimaryKeyConstraint("snapshot_id"),
        sa.UniqueConstraint(
            "source",
            "asset_id",
            "nav_date",
            name="uq_fund_nav_snapshots_source_asset_nav_date",
        ),
    )
    op.create_index(
        "idx_fund_nav_snapshots_asset_nav_date",
        "fund_nav_snapshots",
        ["asset_id", "nav_date"],
    )
    op.create_index(
        "idx_fund_nav_snapshots_symbol_nav_date",
        "fund_nav_snapshots",
        ["symbol", "nav_date"],
    )
    op.create_index("idx_fund_nav_snapshots_status", "fund_nav_snapshots", ["status"])
    op.create_index("idx_fund_nav_snapshots_source", "fund_nav_snapshots", ["source"])
    op.execute(
        """
        COMMENT ON TABLE fund_nav_snapshots IS
        '开放式基金净值快照表，独立保存单位净值、累计净值和申赎状态，不与股票式 K 线混用。';
        COMMENT ON COLUMN fund_nav_snapshots.snapshot_id IS '稳定唯一净值快照 ID。';
        COMMENT ON COLUMN fund_nav_snapshots.asset_id IS '基金资产 ID，例如 fund:open:000001。';
        COMMENT ON COLUMN fund_nav_snapshots.symbol IS '基金代码。';
        COMMENT ON COLUMN fund_nav_snapshots.market IS '市场标识，固定为 fund。';
        COMMENT ON COLUMN fund_nav_snapshots.nav_date IS '净值对应日期。';
        COMMENT ON COLUMN fund_nav_snapshots.unit_nav IS '单位净值。';
        COMMENT ON COLUMN fund_nav_snapshots.accumulated_nav IS '累计净值。';
        COMMENT ON COLUMN fund_nav_snapshots.daily_return IS '日涨跌幅，小数表示，例如 0.0123。';
        COMMENT ON COLUMN fund_nav_snapshots.purchase_status IS '申购状态。';
        COMMENT ON COLUMN fund_nav_snapshots.redeem_status IS '赎回状态。';
        COMMENT ON COLUMN fund_nav_snapshots.source IS '来源接口标识。';
        COMMENT ON COLUMN fund_nav_snapshots.status IS '数据状态，例如 available、partial、error。';
        COMMENT ON COLUMN fund_nav_snapshots.payload IS '原始字段和扩展信息。';
        COMMENT ON COLUMN fund_nav_snapshots.created_at IS '入库时间。';
        COMMENT ON COLUMN fund_nav_snapshots.updated_at IS '最近更新时间。';
        """
    )


def downgrade() -> None:
    op.drop_index("idx_fund_nav_snapshots_source", table_name="fund_nav_snapshots")
    op.drop_index("idx_fund_nav_snapshots_status", table_name="fund_nav_snapshots")
    op.drop_index("idx_fund_nav_snapshots_symbol_nav_date", table_name="fund_nav_snapshots")
    op.drop_index("idx_fund_nav_snapshots_asset_nav_date", table_name="fund_nav_snapshots")
    op.drop_table("fund_nav_snapshots")
