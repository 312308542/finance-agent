"""创建 A 股 M1 基础数据表

Revision ID: 20260515_0003
Revises: 20260514_0002
Create Date: 2026-05-15 00:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260515_0003"
down_revision = "20260514_0002"
branch_labels = None
depends_on = None


def _jsonb_object() -> sa.Column:
    return sa.Column(
        "payload",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "fundamental_snapshots",
        sa.Column("snapshot_id", sa.String(length=192), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("report_period", sa.String(length=32), nullable=True),
        sa.Column("pe_ttm", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("pb", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("roe", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("revenue_growth_yoy", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("net_profit_growth_yoy", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("debt_to_asset", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("operating_cashflow", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "missing_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )
    op.create_index(
        "idx_fundamental_asset_period",
        "fundamental_snapshots",
        ["asset_id", "report_period"],
    )
    op.create_index("idx_fundamental_as_of", "fundamental_snapshots", ["as_of"])
    op.create_index("idx_fundamental_status", "fundamental_snapshots", ["status"])
    op.create_index("idx_fundamental_source", "fundamental_snapshots", ["source"])

    op.create_table(
        "capital_flow_snapshots",
        sa.Column("snapshot_id", sa.String(length=192), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("main_net_inflow", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("northbound_net_inflow", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("turnover_rate", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("amount", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("window", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )
    op.create_index(
        "idx_capital_flow_asset_window_asof",
        "capital_flow_snapshots",
        ["asset_id", "window", "as_of"],
    )
    op.create_index(
        "idx_capital_flow_symbol_asof",
        "capital_flow_snapshots",
        ["symbol", "as_of"],
    )
    op.create_index("idx_capital_flow_source", "capital_flow_snapshots", ["source"])

    op.create_table(
        "event_records",
        sa.Column("event_id", sa.String(length=192), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("sentiment", sa.String(length=32), nullable=False),
        sa.Column("importance", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "idx_events_asset_published",
        "event_records",
        ["asset_id", "published_at"],
    )
    op.create_index("idx_events_market_type", "event_records", ["market", "event_type"])
    op.create_index("idx_events_importance", "event_records", ["importance"])
    op.create_index("idx_events_source", "event_records", ["source"])


def downgrade() -> None:
    op.drop_table("event_records")
    op.drop_table("capital_flow_snapshots")
    op.drop_table("fundamental_snapshots")
