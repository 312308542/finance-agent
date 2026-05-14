"""创建数字货币衍生品快照表

Revision ID: 20260514_0002
Revises: 20260514_0001
Create Date: 2026-05-14 03:45:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260514_0002"
down_revision = "20260514_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crypto_derivative_snapshots",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=192), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("funding_rate", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column("next_funding_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open_interest", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("open_interest_value", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("long_short_ratio", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("basis_rate", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column("liquidation_risk_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False
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
        sa.PrimaryKeyConstraint("asset_id", "as_of", "source"),
    )
    op.create_index(
        "idx_crypto_derivatives_snapshot_id",
        "crypto_derivative_snapshots",
        ["snapshot_id"],
    )
    op.create_index(
        "idx_crypto_derivatives_asset_asof",
        "crypto_derivative_snapshots",
        ["asset_id", "as_of"],
    )
    op.create_index(
        "idx_crypto_derivatives_symbol_asof",
        "crypto_derivative_snapshots",
        ["symbol", "as_of"],
    )
    op.create_index(
        "idx_crypto_derivatives_status",
        "crypto_derivative_snapshots",
        ["status"],
    )
    op.execute(
        """
        SELECT create_hypertable(
          'crypto_derivative_snapshots',
          'as_of',
          if_not_exists => TRUE,
          chunk_time_interval => INTERVAL '7 days'
        )
        """
    )
    op.execute(
        """
        ALTER TABLE crypto_derivative_snapshots SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'asset_id',
          timescaledb.compress_orderby = 'as_of DESC'
        )
        """
    )


def downgrade() -> None:
    op.drop_table("crypto_derivative_snapshots")
