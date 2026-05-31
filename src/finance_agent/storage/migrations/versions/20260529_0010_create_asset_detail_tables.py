"""创建资产详情附表

Revision ID: 20260529_0010
Revises: 20260521_0009
Create Date: 2026-05-29 10:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260529_0010"
down_revision = "20260521_0009"
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
        "asset_profiles",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("sector", sa.String(length=128), nullable=True),
        sa.Column("industry", sa.String(length=128), nullable=True),
        sa.Column("concept", sa.String(length=128), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
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
        sa.PrimaryKeyConstraint("asset_id", "source"),
    )
    op.create_index("idx_asset_profiles_asset_source", "asset_profiles", ["asset_id", "source"])
    op.create_index("idx_asset_profiles_market_symbol", "asset_profiles", ["market", "symbol"])
    op.create_index("idx_asset_profiles_sector", "asset_profiles", ["sector"])

    op.create_table(
        "asset_provider_mappings",
        sa.Column("mapping_id", sa.String(length=192), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_symbol", sa.String(length=128), nullable=False),
        sa.Column("provider_exchange", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False
        ),
        _jsonb_object(),
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
        sa.PrimaryKeyConstraint("mapping_id"),
        sa.UniqueConstraint(
            "provider",
            "provider_symbol",
            "market",
            name="uq_asset_provider_mappings_provider_symbol_market",
        ),
    )
    op.create_index("idx_asset_provider_mappings_asset", "asset_provider_mappings", ["asset_id"])
    op.create_index(
        "idx_asset_provider_mappings_provider",
        "asset_provider_mappings",
        ["provider", "source"],
    )

    op.create_table(
        "asset_status_snapshots",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("tradable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("trading_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _jsonb_object(),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("asset_id", "as_of", "source"),
    )
    op.create_index("idx_asset_status_asset_asof", "asset_status_snapshots", ["asset_id", "as_of"])
    op.create_index(
        "idx_asset_status_market_symbol_asof",
        "asset_status_snapshots",
        ["market", "symbol", "as_of"],
    )
    op.create_index("idx_asset_status_status", "asset_status_snapshots", ["trading_status"])

    op.create_table(
        "realtime_quote_snapshots",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("last_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("prev_close", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("open", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("high", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("low", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("volume", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("amount", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("turnover_rate", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("change_amount", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("change_percent", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("bid_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("ask_price", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False
        ),
        _jsonb_object(),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("asset_id", "as_of", "source"),
    )
    op.create_index(
        "idx_realtime_quotes_asset_asof",
        "realtime_quote_snapshots",
        ["asset_id", "as_of"],
    )
    op.create_index(
        "idx_realtime_quotes_market_symbol_asof",
        "realtime_quote_snapshots",
        ["market", "symbol", "as_of"],
    )
    op.create_index("idx_realtime_quotes_source", "realtime_quote_snapshots", ["source"])


def downgrade() -> None:
    op.drop_table("realtime_quote_snapshots")
    op.drop_table("asset_status_snapshots")
    op.drop_table("asset_provider_mappings")
    op.drop_table("asset_profiles")
