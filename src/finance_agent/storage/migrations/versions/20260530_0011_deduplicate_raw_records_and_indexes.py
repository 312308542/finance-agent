"""原始响应去重和基础数据索引优化

Revision ID: 20260530_0011
Revises: 20260529_0010
Create Date: 2026-05-30 10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260530_0011"
down_revision = "20260529_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先把直接引用 raw_records 的标准 K 线指向保留行，再删除精确重复的原始响应。
    op.execute(
        """
        WITH ranked AS (
            SELECT
                raw_record_id,
                first_value(raw_record_id) OVER (
                    PARTITION BY provider, endpoint, request_hash, content_hash, status
                    ORDER BY collected_at DESC, raw_record_id DESC
                ) AS keep_raw_record_id
            FROM raw_records
            WHERE request_hash IS NOT NULL
              AND content_hash IS NOT NULL
        )
        UPDATE market_bars AS bar
        SET raw_record_id = ranked.keep_raw_record_id
        FROM ranked
        WHERE bar.raw_record_id = ranked.raw_record_id
          AND ranked.raw_record_id <> ranked.keep_raw_record_id
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT
                raw_record_id,
                row_number() OVER (
                    PARTITION BY provider, endpoint, request_hash, content_hash, status
                    ORDER BY collected_at DESC, raw_record_id DESC
                ) AS rn
            FROM raw_records
            WHERE request_hash IS NOT NULL
              AND content_hash IS NOT NULL
        )
        DELETE FROM raw_records AS raw
        USING ranked
        WHERE raw.raw_record_id = ranked.raw_record_id
          AND ranked.rn > 1
        """
    )
    op.create_index(
        "uq_raw_records_exact_dedup",
        "raw_records",
        ["provider", "endpoint", "request_hash", "content_hash", "status"],
        unique=True,
    )
    op.create_index(
        "idx_fundamental_asset_source_asof",
        "fundamental_snapshots",
        ["asset_id", "source", "as_of"],
    )
    op.create_index(
        "idx_fundamental_asset_source_period",
        "fundamental_snapshots",
        ["asset_id", "source", "report_period"],
    )
    op.create_index(
        "uq_fundamental_source_asset_asof_valuation",
        "fundamental_snapshots",
        ["source", "asset_id", "as_of"],
        unique=True,
        postgresql_where=sa.text("report_period IS NULL"),
    )
    op.create_index(
        "uq_fundamental_source_asset_period_report",
        "fundamental_snapshots",
        ["source", "asset_id", "report_period"],
        unique=True,
        postgresql_where=sa.text("report_period IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_fundamental_source_asset_period_report",
        table_name="fundamental_snapshots",
    )
    op.drop_index(
        "uq_fundamental_source_asset_asof_valuation",
        table_name="fundamental_snapshots",
    )
    op.drop_index("idx_fundamental_asset_source_period", table_name="fundamental_snapshots")
    op.drop_index("idx_fundamental_asset_source_asof", table_name="fundamental_snapshots")
    op.drop_index("uq_raw_records_exact_dedup", table_name="raw_records")
