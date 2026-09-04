"""强化实时行情历史的点时与质量字段。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260904_0028"
down_revision = "20260831_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "realtime_quote_snapshots",
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "realtime_quote_snapshots",
        sa.Column("freshness_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "realtime_quote_snapshots",
        sa.Column(
            "quality_status",
            sa.String(length=32),
            server_default=sa.text("'available'"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE realtime_quote_snapshots "
        "SET captured_at = created_at "
        "WHERE captured_at IS NULL"
    )
    op.alter_column(
        "realtime_quote_snapshots",
        "captured_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    op.create_index(
        "idx_realtime_quotes_quality_asof",
        "realtime_quote_snapshots",
        ["quality_status", "as_of"],
    )
    op.execute(
        "SELECT create_hypertable('realtime_quote_snapshots', 'as_of', "
        "if_not_exists => TRUE, migrate_data => TRUE)"
    )
    op.execute(
        "SELECT add_retention_policy('realtime_quote_snapshots', INTERVAL '7 days', "
        "if_not_exists => TRUE)"
    )
    op.execute(
        "SELECT add_retention_policy('market_bars_intraday', INTERVAL '180 days', "
        "if_not_exists => TRUE)"
    )


def downgrade() -> None:
    op.execute(
        "SELECT remove_retention_policy('market_bars_intraday', if_exists => TRUE)"
    )
    op.execute(
        "SELECT remove_retention_policy('realtime_quote_snapshots', if_exists => TRUE)"
    )
    op.drop_index(
        "idx_realtime_quotes_quality_asof",
        table_name="realtime_quote_snapshots",
    )
    op.drop_column("realtime_quote_snapshots", "quality_status")
    op.drop_column("realtime_quote_snapshots", "freshness_ms")
    op.drop_column("realtime_quote_snapshots", "captured_at")
