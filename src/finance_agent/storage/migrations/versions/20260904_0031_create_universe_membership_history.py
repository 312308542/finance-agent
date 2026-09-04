"""创建候选池成员有效区间历史表。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260904_0031"
down_revision = "20260904_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_universe_membership_history",
        sa.Column("history_id", sa.String(192), primary_key=True),
        sa.Column("universe_id", sa.String(128), nullable=False),
        sa.Column("asset_id", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("market", sa.String(32), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("status_flags", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("source_snapshot_id", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.UniqueConstraint("universe_id", "asset_id", "valid_from", name="uq_universe_membership_history"),
    )
    op.create_index(
        "idx_universe_membership_history_as_of",
        "asset_universe_membership_history",
        ["universe_id", "valid_from", "valid_to"],
    )
    op.create_index(
        "idx_universe_membership_history_asset",
        "asset_universe_membership_history",
        ["asset_id", "valid_from"],
    )


def downgrade() -> None:
    op.drop_index("idx_universe_membership_history_asset", table_name="asset_universe_membership_history")
    op.drop_index("idx_universe_membership_history_as_of", table_name="asset_universe_membership_history")
    op.drop_table("asset_universe_membership_history")
