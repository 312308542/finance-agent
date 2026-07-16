"""为评分表增加正式策略维度。

Revision ID: 20260716_0021
Revises: 20260630_0020
Create Date: 2026-07-16 16:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260716_0021"
down_revision = "20260630_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """回填历史策略并把评分主键升级为策略化 ID。"""

    op.add_column("asset_scores", sa.Column("strategy_id", sa.String(length=128)))
    op.alter_column(
        "asset_scores",
        "score_id",
        existing_type=sa.String(length=160),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "asset_recommendations",
        "score_id",
        existing_type=sa.String(length=160),
        type_=sa.String(length=255),
        existing_nullable=True,
    )

    op.execute(
        """
        UPDATE asset_scores
        SET strategy_id = COALESCE(
                NULLIF(payload ->> 'strategy_id', ''),
                NULLIF(payload ->> 'score_strategy_id', ''),
                CASE
                    WHEN market = 'ashare' THEN 'strategy:ashare:legacy_default'
                    ELSE 'strategy' || chr(58) || market || chr(58) || 'legacy_default'
                END
            ),
            payload = jsonb_set(
                COALESCE(payload, '{}'::jsonb),
                '{strategy_id}',
                to_jsonb(
                    COALESCE(
                        NULLIF(payload ->> 'strategy_id', ''),
                        NULLIF(payload ->> 'score_strategy_id', ''),
                        CASE
                            WHEN market = 'ashare' THEN 'strategy:ashare:legacy_default'
                            ELSE 'strategy' || chr(58) || market || chr(58) || 'legacy_default'
                        END
                    )
                ),
                true
            )
        """
    )
    op.execute(
        """
        CREATE TEMPORARY TABLE asset_score_strategy_id_map
        ON COMMIT DROP AS
        SELECT
            score_id AS old_score_id,
            score_id || ':strategy:' || LEFT(md5(strategy_id), 12) AS new_score_id
        FROM asset_scores
        """
    )
    op.execute(
        """
        UPDATE asset_recommendations AS recommendation
        SET score_id = mapping.new_score_id
        FROM asset_score_strategy_id_map AS mapping
        WHERE recommendation.score_id = mapping.old_score_id
        """
    )
    op.execute(
        """
        UPDATE asset_scores AS score
        SET score_id = mapping.new_score_id
        FROM asset_score_strategy_id_map AS mapping
        WHERE score.score_id = mapping.old_score_id
        """
    )

    op.alter_column(
        "asset_scores",
        "strategy_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.create_index(
        "idx_asset_scores_screening_strategy_rank",
        "asset_scores",
        ["screening_id", "strategy_id", "rank"],
    )
    op.create_index(
        "idx_asset_scores_asset_strategy_horizon_asof",
        "asset_scores",
        ["asset_id", "strategy_id", "horizon", "as_of"],
    )
    op.execute(
        """
        COMMENT ON COLUMN asset_scores.strategy_id IS
        '评分策略 ID；同一筛选截面可持久化多套互不覆盖的评分。'
        """
    )


def downgrade() -> None:
    """安全还原旧主键；存在多策略冲突时明确拒绝降级。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM asset_scores
                WHERE score_id !~ ':strategy:[0-9a-f]{12}$'
            ) THEN
                RAISE EXCEPTION '拒绝降级：存在不符合策略化格式的评分 ID';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM (
                    SELECT
                        regexp_replace(
                            score_id,
                            ':strategy:[0-9a-f]{12}$',
                            ''
                        ) AS base_score_id
                    FROM asset_scores
                    GROUP BY base_score_id
                    HAVING COUNT(*) > 1
                ) AS conflicts
            ) THEN
                RAISE EXCEPTION '拒绝降级：同一基础评分 ID 已存在多策略记录';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM asset_scores
                WHERE length(
                    regexp_replace(score_id, ':strategy:[0-9a-f]{12}$', '')
                ) > 160
            ) THEN
                RAISE EXCEPTION '拒绝降级：基础评分 ID 超过旧字段长度 160';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TEMPORARY TABLE asset_score_legacy_id_map
        ON COMMIT DROP AS
        SELECT
            score_id AS strategy_score_id,
            regexp_replace(
                score_id,
                ':strategy:[0-9a-f]{12}$',
                ''
            ) AS legacy_score_id
        FROM asset_scores
        """
    )
    op.execute(
        """
        UPDATE asset_recommendations AS recommendation
        SET score_id = mapping.legacy_score_id
        FROM asset_score_legacy_id_map AS mapping
        WHERE recommendation.score_id = mapping.strategy_score_id
        """
    )
    op.execute(
        """
        UPDATE asset_scores AS score
        SET score_id = mapping.legacy_score_id
        FROM asset_score_legacy_id_map AS mapping
        WHERE score.score_id = mapping.strategy_score_id
        """
    )

    op.drop_index(
        "idx_asset_scores_asset_strategy_horizon_asof",
        table_name="asset_scores",
    )
    op.drop_index(
        "idx_asset_scores_screening_strategy_rank",
        table_name="asset_scores",
    )
    op.drop_column("asset_scores", "strategy_id")
    op.alter_column(
        "asset_recommendations",
        "score_id",
        existing_type=sa.String(length=255),
        type_=sa.String(length=160),
        existing_nullable=True,
    )
    op.alter_column(
        "asset_scores",
        "score_id",
        existing_type=sa.String(length=255),
        type_=sa.String(length=160),
        existing_nullable=False,
    )
