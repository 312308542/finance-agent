"""创建策略前向观察账本和试运行状态表。

Revision ID: 20260716_0022
Revises: 20260716_0021
Create Date: 2026-07-16 19:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from finance_agent.scoring.strategies import default_scoring_strategy_seeds

revision = "20260716_0022"
down_revision = "20260716_0021"
branch_labels = None
depends_on = None

MIXED_STRATEGY_ID = "strategy:ashare:short_theme_mixed_v1"
MIXED_STRATEGY_SEED = next(
    item for item in default_scoring_strategy_seeds() if item["strategy_id"] == MIXED_STRATEGY_ID
)


def upgrade() -> None:
    """创建追加式观察账本，并幂等补入固定混合策略。"""

    op.create_table(
        "strategy_observation_runs",
        sa.Column("observation_id", sa.String(length=192), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("universe_id", sa.String(length=128), nullable=False),
        sa.Column("screening_id", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "data_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("observation_id"),
        sa.UniqueConstraint(
            "trade_date",
            "universe_id",
            name="uq_strategy_observation_run_day_universe",
        ),
    )
    op.create_index(
        "idx_strategy_observation_runs_date",
        "strategy_observation_runs",
        ["trade_date"],
    )
    op.create_index(
        "idx_strategy_observation_runs_status",
        "strategy_observation_runs",
        ["status"],
    )

    op.create_table(
        "strategy_observation_positions",
        sa.Column("position_id", sa.String(length=255), nullable=False),
        sa.Column("observation_id", sa.String(length=192), nullable=False),
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score_id", sa.String(length=255), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("benchmark_entry_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("position_id"),
        sa.UniqueConstraint(
            "observation_id",
            "strategy_id",
            "asset_id",
            name="uq_strategy_position_asset",
        ),
    )
    op.create_index(
        "idx_strategy_positions_observation",
        "strategy_observation_positions",
        ["observation_id"],
    )
    op.create_index(
        "idx_strategy_positions_strategy_signal",
        "strategy_observation_positions",
        ["strategy_id", "signal_date"],
    )
    op.create_index(
        "idx_strategy_positions_asset",
        "strategy_observation_positions",
        ["asset_id"],
    )

    op.create_table(
        "strategy_observation_outcomes",
        sa.Column("outcome_id", sa.String(length=255), nullable=False),
        sa.Column("position_id", sa.String(length=255), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("due_trade_date", sa.Date(), nullable=True),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("exit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("gross_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("net_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("benchmark_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("excess_return", sa.Numeric(18, 8), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "horizon_days in (5, 10, 20)",
            name="ck_strategy_outcome_horizon",
        ),
        sa.PrimaryKeyConstraint("outcome_id"),
        sa.UniqueConstraint(
            "position_id",
            "horizon_days",
            name="uq_strategy_outcome_horizon",
        ),
    )
    op.create_index(
        "idx_strategy_outcomes_due_status",
        "strategy_observation_outcomes",
        ["due_trade_date", "status"],
    )
    op.create_index(
        "idx_strategy_outcomes_position",
        "strategy_observation_outcomes",
        ["position_id"],
    )

    op.create_table(
        "strategy_trial_states",
        sa.Column("strategy_id", sa.String(length=128), nullable=False),
        sa.Column("strategy_version", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("historical_evidence_id", sa.String(length=192), nullable=True),
        sa.Column(
            "forward_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "consecutive_failure_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state in ('research','historical_passed','trial','validated','disabled')",
            name="ck_strategy_trial_state",
        ),
        sa.PrimaryKeyConstraint("strategy_id"),
    )
    op.create_index(
        "idx_strategy_trial_states_state",
        "strategy_trial_states",
        ["state"],
    )

    strategy_table = sa.table(
        "scoring_strategies",
        sa.column("strategy_id", sa.String),
        sa.column("market", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("group_weights", postgresql.JSONB),
        sa.column("missing_penalty", postgresql.JSONB),
        sa.column("status", sa.String),
    )
    op.execute(
        postgresql.insert(strategy_table)
        .values(**MIXED_STRATEGY_SEED)
        .on_conflict_do_nothing(index_elements=["strategy_id"])
    )


def downgrade() -> None:
    """无观察或评分引用时删除观察表和自动补入的混合策略。"""

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM asset_scores WHERE strategy_id = '{MIXED_STRATEGY_ID}'
            ) OR EXISTS (
                SELECT 1 FROM backtest_results WHERE strategy_id = '{MIXED_STRATEGY_ID}'
            ) OR EXISTS (
                SELECT 1 FROM strategy_observation_positions
                WHERE strategy_id = '{MIXED_STRATEGY_ID}'
            ) OR EXISTS (
                SELECT 1 FROM strategy_trial_states
                WHERE strategy_id = '{MIXED_STRATEGY_ID}'
            ) THEN
                RAISE EXCEPTION '拒绝降级：固定混合策略已有评分、回测或观察证据引用';
            END IF;
        END
        $$
        """
    )
    op.execute(
        sa.text("DELETE FROM scoring_strategies WHERE strategy_id = :strategy_id").bindparams(
            strategy_id=MIXED_STRATEGY_ID
        )
    )

    op.drop_index("idx_strategy_trial_states_state", table_name="strategy_trial_states")
    op.drop_table("strategy_trial_states")
    op.drop_index(
        "idx_strategy_outcomes_position",
        table_name="strategy_observation_outcomes",
    )
    op.drop_index(
        "idx_strategy_outcomes_due_status",
        table_name="strategy_observation_outcomes",
    )
    op.drop_table("strategy_observation_outcomes")
    op.drop_index(
        "idx_strategy_positions_asset",
        table_name="strategy_observation_positions",
    )
    op.drop_index(
        "idx_strategy_positions_strategy_signal",
        table_name="strategy_observation_positions",
    )
    op.drop_index(
        "idx_strategy_positions_observation",
        table_name="strategy_observation_positions",
    )
    op.drop_table("strategy_observation_positions")
    op.drop_index(
        "idx_strategy_observation_runs_status",
        table_name="strategy_observation_runs",
    )
    op.drop_index(
        "idx_strategy_observation_runs_date",
        table_name="strategy_observation_runs",
    )
    op.drop_table("strategy_observation_runs")
