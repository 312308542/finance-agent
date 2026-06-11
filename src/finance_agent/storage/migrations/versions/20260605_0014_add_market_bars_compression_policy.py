"""为行情 K 线表增加自动压缩策略。

Revision ID: 20260605_0014
Revises: 20260604_0013
Create Date: 2026-06-05 14:30:00
"""

from __future__ import annotations

from alembic import op

revision = "20260605_0014"
down_revision = "20260604_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM add_compression_policy(
                    'market_bars',
                    INTERVAL '90 days',
                    if_not_exists => TRUE
                );
            END IF;
        EXCEPTION
            WHEN undefined_function OR duplicate_object THEN
                NULL;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                PERFORM remove_compression_policy('market_bars', if_exists => TRUE);
            END IF;
        EXCEPTION
            WHEN undefined_function THEN
                NULL;
        END $$;
        """
    )
