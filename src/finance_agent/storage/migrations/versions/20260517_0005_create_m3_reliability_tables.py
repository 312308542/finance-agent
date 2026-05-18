"""创建 M3 数据可靠性和推荐入池辅助表

Revision ID: 20260517_0005
Revises: 20260517_0004
Create Date: 2026-05-17 16:10:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260517_0005"
down_revision = "20260517_0004"
branch_labels = None
depends_on = None


def _jsonb_object(name: str = "payload") -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def _jsonb_array(name: str) -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'[]'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("snapshot_id", sa.String(length=192), nullable=False),
        sa.Column("portfolio_id", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("total_equity", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("cash", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("market_value", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("position_count", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("snapshot_id", "captured_at"),
    )
    op.create_index(
        "idx_portfolio_snapshots_portfolio_time",
        "portfolio_snapshots",
        ["portfolio_id", "captured_at"],
    )
    op.create_index(
        "idx_portfolio_snapshots_owner_time",
        "portfolio_snapshots",
        ["owner_id", "captured_at"],
    )
    op.execute(
        """
        SELECT create_hypertable(
          'portfolio_snapshots',
          'captured_at',
          if_not_exists => TRUE,
          migrate_data => TRUE
        )
        """
    )

    op.create_table(
        "position_snapshots",
        sa.Column("snapshot_id", sa.String(length=192), nullable=False),
        sa.Column("position_id", sa.String(length=160), nullable=False),
        sa.Column("portfolio_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=36, scale=10), nullable=False),
        sa.Column("avg_cost", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("last_price", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("market_value", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("unrealized_pnl_pct", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("portfolio_weight", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("leverage", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("liquidation_price", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("snapshot_id", "captured_at"),
    )
    op.create_index(
        "idx_position_snapshots_position_time",
        "position_snapshots",
        ["position_id", "captured_at"],
    )
    op.create_index(
        "idx_position_snapshots_portfolio_time",
        "position_snapshots",
        ["portfolio_id", "captured_at"],
    )
    op.create_index(
        "idx_position_snapshots_asset_time",
        "position_snapshots",
        ["asset_id", "captured_at"],
    )
    op.execute(
        """
        SELECT create_hypertable(
          'position_snapshots',
          'captured_at',
          if_not_exists => TRUE,
          migrate_data => TRUE
        )
        """
    )

    op.create_table(
        "watchlist_item_events",
        sa.Column("event_id", sa.String(length=192), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("watchlist_id", sa.String(length=128), nullable=False),
        sa.Column("watchlist_item_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source_decision_id", sa.String(length=160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "idx_watchlist_events_watchlist_created",
        "watchlist_item_events",
        ["watchlist_id", "created_at"],
    )
    op.create_index(
        "idx_watchlist_events_item_created",
        "watchlist_item_events",
        ["watchlist_item_id", "created_at"],
    )
    op.create_index(
        "idx_watchlist_events_asset_created",
        "watchlist_item_events",
        ["asset_id", "created_at"],
    )
    op.create_index(
        "idx_watchlist_events_owner_created",
        "watchlist_item_events",
        ["owner_id", "created_at"],
    )

    op.create_table(
        "data_quality_snapshots",
        sa.Column("quality_id", sa.String(length=192), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("data_domain", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("freshness_status", sa.String(length=32), nullable=False),
        sa.Column("latest_data_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb_array("missing_items"),
        sa.Column("issue_count", sa.Integer(), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("quality_id", "checked_at"),
    )
    op.create_index(
        "idx_quality_asset_domain_checked",
        "data_quality_snapshots",
        ["asset_id", "data_domain", "checked_at"],
    )
    op.create_index(
        "idx_quality_market_domain",
        "data_quality_snapshots",
        ["market", "data_domain"],
    )
    op.create_index(
        "idx_quality_status",
        "data_quality_snapshots",
        ["status", "freshness_status"],
    )
    op.execute(
        """
        SELECT create_hypertable(
          'data_quality_snapshots',
          'checked_at',
          if_not_exists => TRUE,
          migrate_data => TRUE
        )
        """
    )


def downgrade() -> None:
    op.drop_table("data_quality_snapshots")
    op.drop_table("watchlist_item_events")
    op.drop_table("position_snapshots")
    op.drop_table("portfolio_snapshots")
