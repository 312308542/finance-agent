"""新增数据采集水位表

Revision ID: 20260604_0013
Revises: 20260604_0012
Create Date: 2026-06-04 03:45:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260604_0013"
down_revision = "20260604_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_sync_watermarks",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("data_domain", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), server_default="", nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fail_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_message", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint(
            "asset_id",
            "data_domain",
            "timeframe",
            "provider",
            name="pk_data_sync_watermarks",
        ),
    )
    op.create_index(
        "idx_data_sync_watermarks_market_domain",
        "data_sync_watermarks",
        ["market", "data_domain"],
    )
    op.create_index(
        "idx_data_sync_watermarks_next_retry",
        "data_sync_watermarks",
        ["next_retry_at"],
    )
    op.create_index(
        "idx_data_sync_watermarks_status",
        "data_sync_watermarks",
        ["status"],
    )
    op.create_index(
        "idx_data_sync_watermarks_watermark",
        "data_sync_watermarks",
        ["data_domain", "timeframe", "watermark_at"],
    )
    op.execute(
        sa.text(
            "COMMENT ON TABLE data_sync_watermarks IS "
            "'数据采集水位表，记录每个资产在各数据域的成功水位、失败次数和下次重试时间。'"
        )
    )
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.asset_id IS '资产唯一标识。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.market IS '资产所属市场。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.symbol IS '资产代码。'"))
    op.execute(
        sa.text(
            "COMMENT ON COLUMN data_sync_watermarks.data_domain IS "
            "'数据域，例如 market_bars、stock_news、fundamentals。'"
        )
    )
    op.execute(
        sa.text(
            "COMMENT ON COLUMN data_sync_watermarks.timeframe IS "
            "'周期，例如 1d、1h；无周期数据使用空字符串。'"
        )
    )
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.provider IS '数据源或 Provider 标识。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.status IS '当前采集状态。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.watermark_at IS '已成功采集到的数据时间水位。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.last_success_at IS '最近一次成功采集时间。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.last_failed_at IS '最近一次失败采集时间。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.next_retry_at IS '下一次允许重试的时间。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.fail_count IS '连续失败次数。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.last_error_message IS '最近一次失败原因。'"))
    op.execute(sa.text("COMMENT ON COLUMN data_sync_watermarks.payload IS '扩展状态信息。'"))


def downgrade() -> None:
    op.drop_index("idx_data_sync_watermarks_watermark", table_name="data_sync_watermarks")
    op.drop_index("idx_data_sync_watermarks_status", table_name="data_sync_watermarks")
    op.drop_index("idx_data_sync_watermarks_next_retry", table_name="data_sync_watermarks")
    op.drop_index("idx_data_sync_watermarks_market_domain", table_name="data_sync_watermarks")
    op.drop_table("data_sync_watermarks")
