"""创建 A 股持仓盘中监控状态和追加事件表。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260904_0030"
down_revision = "20260904_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "position_monitoring_states",
        sa.Column("monitoring_state_id", sa.String(255), primary_key=True),
        sa.Column("position_id", sa.String(255), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("portfolio_id", sa.String(160), nullable=False),
        sa.Column("asset_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("market", sa.String(32), nullable=False),
        sa.Column("setup_id", sa.String(192)),
        sa.Column("decision_snapshot_id", sa.String(255)),
        sa.Column("current_action", sa.String(32), nullable=False),
        sa.Column("previous_valid_action", sa.String(32), nullable=False),
        sa.Column("cost_price", sa.Numeric(18, 8)),
        sa.Column("opened_on", sa.Date()),
        sa.Column("total_quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("sellable_quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("active_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("planned_horizon_days", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("invalidation_price", sa.Numeric(18, 8)),
        sa.Column("protective_price", sa.Numeric(18, 8)),
        sa.Column("highest_price", sa.Numeric(18, 8)),
        sa.Column("sector_id", sa.String(128)),
        sa.Column("sector_regime", sa.String(32), server_default=sa.text("'unknown'"), nullable=False),
        sa.Column("last_quote_at", sa.DateTime(timezone=True)),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.UniqueConstraint("position_id", name="uq_position_monitoring_position"),
    )
    op.create_index(
        "idx_position_monitoring_owner_action",
        "position_monitoring_states",
        ["owner_id", "current_action"],
    )
    op.create_index(
        "idx_position_monitoring_quote",
        "position_monitoring_states",
        ["last_quote_at"],
    )
    op.create_table(
        "position_monitoring_events",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("monitoring_state_id", sa.String(255), nullable=False),
        sa.Column("position_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("quote_snapshot_id", sa.String(255)),
        sa.Column("decision_snapshot_id", sa.String(255)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.create_index(
        "idx_position_monitoring_events_position_time",
        "position_monitoring_events",
        ["position_id", "occurred_at"],
    )
    op.create_index(
        "idx_position_monitoring_events_state_time",
        "position_monitoring_events",
        ["monitoring_state_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_position_monitoring_events_state_time", table_name="position_monitoring_events")
    op.drop_index("idx_position_monitoring_events_position_time", table_name="position_monitoring_events")
    op.drop_table("position_monitoring_events")
    op.drop_index("idx_position_monitoring_quote", table_name="position_monitoring_states")
    op.drop_index("idx_position_monitoring_owner_action", table_name="position_monitoring_states")
    op.drop_table("position_monitoring_states")
