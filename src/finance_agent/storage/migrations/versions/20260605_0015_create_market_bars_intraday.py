"""创建盘中 K 线独立表。

Revision ID: 20260605_0015
Revises: 20260605_0014
Create Date: 2026-06-05 15:20:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260605_0015"
down_revision = "20260605_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_bars_intraday",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("high", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("low", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("close", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("volume", sa.Numeric(precision=36, scale=10), nullable=False),
        sa.Column("amount", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("adjustment", sa.String(length=16), server_default=sa.text("''"), nullable=False),
        sa.Column("is_closed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("raw_record_id", sa.String(length=192), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("asset_id", "timeframe", "timestamp", "source", "adjustment"),
    )
    op.create_index(
        "idx_market_bars_intraday_asset_tf_time",
        "market_bars_intraday",
        ["asset_id", "timeframe", "timestamp"],
    )
    op.create_index(
        "idx_market_bars_intraday_market_symbol",
        "market_bars_intraday",
        ["market", "symbol"],
    )
    op.create_index(
        "idx_market_bars_intraday_timestamp",
        "market_bars_intraday",
        ["timestamp"],
    )
    op.create_index(
        "idx_market_bars_intraday_closed",
        "market_bars_intraday",
        ["asset_id", "timeframe", "is_closed", "timestamp"],
    )
    op.execute(
        """
        SELECT create_hypertable(
          'market_bars_intraday',
          'timestamp',
          if_not_exists => TRUE,
          chunk_time_interval => INTERVAL '7 days'
        )
        """
    )
    op.execute(
        """
        ALTER TABLE market_bars_intraday SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'asset_id,timeframe,source',
          timescaledb.compress_orderby = 'timestamp DESC'
        )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM add_compression_policy(
                    'market_bars_intraday',
                    INTERVAL '7 days',
                    if_not_exists => TRUE
                );
            END IF;
        EXCEPTION
            WHEN undefined_function OR duplicate_object THEN
                NULL;
        END $$;
        """
    )
    op.execute(
        """
        COMMENT ON TABLE market_bars_intraday IS
        '分钟/小时级盘中 K 线表，和长期日 K market_bars 分离，使用独立压缩和保留策略。';
        COMMENT ON COLUMN market_bars_intraday.asset_id IS '资产 ID，例如 ashare:600519。';
        COMMENT ON COLUMN market_bars_intraday.symbol IS '交易代码。';
        COMMENT ON COLUMN market_bars_intraday.market IS '市场标识，例如 ashare、fund、crypto_spot。';
        COMMENT ON COLUMN market_bars_intraday.timeframe IS '盘中周期，例如 1m、5m、15m、30m、1h。';
        COMMENT ON COLUMN market_bars_intraday.timestamp IS 'K 线开始时间，TimescaleDB 时间分区列。';
        COMMENT ON COLUMN market_bars_intraday.end_timestamp IS 'K 线结束时间。';
        COMMENT ON COLUMN market_bars_intraday.open IS '开盘价。';
        COMMENT ON COLUMN market_bars_intraday.high IS '最高价。';
        COMMENT ON COLUMN market_bars_intraday.low IS '最低价。';
        COMMENT ON COLUMN market_bars_intraday.close IS '收盘价或当前价。';
        COMMENT ON COLUMN market_bars_intraday.volume IS '成交量。';
        COMMENT ON COLUMN market_bars_intraday.amount IS '成交额。';
        COMMENT ON COLUMN market_bars_intraday.source IS '标准化数据源。';
        COMMENT ON COLUMN market_bars_intraday.adjustment IS '复权方式，盘中一般为空。';
        COMMENT ON COLUMN market_bars_intraday.is_closed IS '该根盘中 K 是否已闭合。';
        COMMENT ON COLUMN market_bars_intraday.raw_record_id IS '对应 raw_records 原始响应 ID。';
        COMMENT ON COLUMN market_bars_intraday.status IS '数据状态，例如 available、partial、revised、error。';
        COMMENT ON COLUMN market_bars_intraday.created_at IS '入库时间。';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM remove_compression_policy(
                    'market_bars_intraday',
                    if_exists => TRUE
                );
            END IF;
        EXCEPTION
            WHEN undefined_function THEN
                NULL;
        END $$;
        """
    )
    op.drop_index("idx_market_bars_intraday_closed", table_name="market_bars_intraday")
    op.drop_index("idx_market_bars_intraday_timestamp", table_name="market_bars_intraday")
    op.drop_index("idx_market_bars_intraday_market_symbol", table_name="market_bars_intraday")
    op.drop_index("idx_market_bars_intraday_asset_tf_time", table_name="market_bars_intraday")
    op.drop_table("market_bars_intraday")
