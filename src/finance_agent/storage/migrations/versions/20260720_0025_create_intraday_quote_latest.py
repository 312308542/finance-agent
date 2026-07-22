"""创建覆盖式盘中临时行情表。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260720_0025"
down_revision = "20260720_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intraday_quote_latest",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("data_snapshot_id", sa.String(length=255), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freshness_ms", sa.Integer(), nullable=True),
        sa.Column("last_price", sa.Numeric(30, 10), nullable=True),
        sa.Column("prev_close", sa.Numeric(30, 10), nullable=True),
        sa.Column("open", sa.Numeric(30, 10), nullable=True),
        sa.Column("high", sa.Numeric(30, 10), nullable=True),
        sa.Column("low", sa.Numeric(30, 10), nullable=True),
        sa.Column("volume", sa.Numeric(36, 10), nullable=True),
        sa.Column("amount", sa.Numeric(36, 10), nullable=True),
        sa.Column("turnover_rate", sa.Numeric(18, 8), nullable=True),
        sa.Column("change_amount", sa.Numeric(30, 10), nullable=True),
        sa.Column("change_percent", sa.Numeric(18, 8), nullable=True),
        sa.Column("bid_price", sa.Numeric(30, 10), nullable=True),
        sa.Column("ask_price", sa.Numeric(30, 10), nullable=True),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False),
        sa.Column("quality_status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
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
    op.create_index(
        "idx_intraday_quote_latest_market",
        "intraday_quote_latest",
        ["market", "updated_at"],
    )
    op.create_index(
        "idx_intraday_quote_latest_snapshot",
        "intraday_quote_latest",
        ["data_snapshot_id"],
    )
    op.create_index(
        "idx_intraday_quote_latest_quality",
        "intraday_quote_latest",
        ["quality_status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_intraday_quote_latest_quality", table_name="intraday_quote_latest")
    op.drop_index("idx_intraday_quote_latest_snapshot", table_name="intraday_quote_latest")
    op.drop_index("idx_intraday_quote_latest_market", table_name="intraday_quote_latest")
    op.drop_table("intraday_quote_latest")
