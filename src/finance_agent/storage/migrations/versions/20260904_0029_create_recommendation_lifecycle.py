"""创建股票设置和推荐生命周期状态表。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260904_0029"
down_revision = "20260904_0028"
branch_labels = None
depends_on = None

RECOMMENDATION_STATE_SQL = (
    "'discovered','watch','setup_confirming','buy_ready','active','weakening',"
    "'exit_pending','exited','cooldown'"
)


def upgrade() -> None:
    op.create_table(
        "stock_setups",
        sa.Column("setup_id", sa.String(length=192), primary_key=True),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("decision_snapshot_id", sa.String(length=255), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("setup_type", sa.String(length=32), nullable=False),
        sa.Column("planned_horizon_days", sa.Integer(), nullable=False),
        sa.Column("entry_zone", postgresql.JSONB(), nullable=False),
        sa.Column("invalidation_price", sa.Numeric(30, 10), nullable=True),
        sa.Column("target_zone", postgresql.JSONB(), nullable=False),
        sa.Column("expected_net_return", sa.Numeric(12, 6), nullable=True),
        sa.Column("downside_risk", sa.Numeric(12, 6), nullable=True),
        sa.Column("confidence", sa.Numeric(12, 6), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_stock_setups_owner_asset", "stock_setups", ["owner_id", "asset_id"])
    op.create_index(
        "idx_stock_setups_snapshot_strategy",
        "stock_setups",
        ["decision_snapshot_id", "strategy_id"],
    )

    op.create_table(
        "recommendation_lifecycle_states",
        sa.Column("state_id", sa.String(length=255), primary_key=True),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("setup_id", sa.String(length=192), nullable=True),
        sa.Column("current_state", sa.String(length=32), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=True),
        sa.Column("decision_snapshot_id", sa.String(length=255), nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consecutive_valid_closes",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("active_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cooldown_until", sa.Date(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
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
        sa.CheckConstraint(
            f"current_state IN ({RECOMMENDATION_STATE_SQL})",
            name="ck_recommendation_lifecycle_current_state",
        ),
        sa.UniqueConstraint(
            "owner_id",
            "strategy_id",
            "asset_id",
            name="uq_recommendation_lifecycle_owner_strategy_asset",
        ),
    )
    op.create_index(
        "idx_recommendation_lifecycle_state",
        "recommendation_lifecycle_states",
        ["current_state", "updated_at"],
    )

    op.create_table(
        "recommendation_lifecycle_events",
        sa.Column("event_id", sa.String(length=255), primary_key=True),
        sa.Column("state_id", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("setup_id", sa.String(length=192), nullable=True),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("decision_snapshot_id", sa.String(length=255), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"to_state IN ({RECOMMENDATION_STATE_SQL})",
            name="ck_recommendation_lifecycle_event_to_state",
        ),
    )
    op.create_index(
        "idx_recommendation_lifecycle_events_state_time",
        "recommendation_lifecycle_events",
        ["state_id", "occurred_at"],
    )
    op.create_index(
        "idx_recommendation_lifecycle_events_owner_asset",
        "recommendation_lifecycle_events",
        ["owner_id", "asset_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_recommendation_lifecycle_events_owner_asset",
        table_name="recommendation_lifecycle_events",
    )
    op.drop_index(
        "idx_recommendation_lifecycle_events_state_time",
        table_name="recommendation_lifecycle_events",
    )
    op.drop_table("recommendation_lifecycle_events")
    op.drop_index(
        "idx_recommendation_lifecycle_state",
        table_name="recommendation_lifecycle_states",
    )
    op.drop_table("recommendation_lifecycle_states")
    op.drop_index("idx_stock_setups_snapshot_strategy", table_name="stock_setups")
    op.drop_index("idx_stock_setups_owner_asset", table_name="stock_setups")
    op.drop_table("stock_setups")
