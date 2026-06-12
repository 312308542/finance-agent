"""创建人工确认与操作闭环表。

Revision ID: 20260613_0018
Revises: 20260612_0017
Create Date: 2026-06-13 10:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260613_0018"
down_revision = "20260612_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "order_drafts",
        sa.Column("order_draft_id", sa.String(length=192), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("decision_log_id", sa.String(length=160), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column(
            "suggested_price_range",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("suggested_position_ratio", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "constraints",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'drafted'"),
            nullable=False,
        ),
        sa.Column("disclaimer", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("order_draft_id"),
        sa.CheckConstraint(
            "action IN ('buy', 'sell', 'add', 'reduce')",
            name="ck_order_drafts_action",
        ),
        sa.CheckConstraint(
            "status IN ('drafted', 'superseded', 'cancelled')",
            name="ck_order_drafts_status",
        ),
        sa.CheckConstraint(
            "length(btrim(disclaimer)) > 0",
            name="ck_order_drafts_disclaimer_nonempty",
        ),
    )
    op.create_index("idx_order_drafts_owner_status", "order_drafts", ["owner_id", "status"])
    op.create_index(
        "idx_order_drafts_decision_status",
        "order_drafts",
        ["decision_log_id", "status"],
    )
    op.create_index(
        "idx_order_drafts_asset_created",
        "order_drafts",
        ["asset_id", "created_at"],
    )

    op.create_table(
        "execution_records",
        sa.Column("execution_id", sa.String(length=192), nullable=False),
        sa.Column("owner_id", sa.String(length=128), nullable=False),
        sa.Column("portfolio_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("order_draft_id", sa.String(length=192), nullable=True),
        sa.Column("decision_log_id", sa.String(length=160), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("executed_price", sa.Numeric(precision=36, scale=10), nullable=False),
        sa.Column("executed_quantity", sa.Numeric(precision=36, scale=10), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fee", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.String(length=64),
            server_default=sa.text("'user_reported'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.CheckConstraint(
            "action IN ('buy', 'sell', 'add', 'reduce')",
            name="ck_execution_records_action",
        ),
        sa.CheckConstraint(
            "source = 'user_reported'",
            name="ck_execution_records_source_user_reported",
        ),
    )
    op.create_index(
        "idx_execution_records_owner_created",
        "execution_records",
        ["owner_id", "created_at"],
    )
    op.create_index(
        "idx_execution_records_asset_executed",
        "execution_records",
        ["asset_id", "executed_at"],
    )
    op.create_index(
        "idx_execution_records_draft",
        "execution_records",
        ["order_draft_id"],
    )
    op.create_index(
        "idx_execution_records_decision",
        "execution_records",
        ["decision_log_id"],
    )

    op.execute(
        """
        COMMENT ON TABLE order_drafts IS
        '订单草案表，仅保存用户确认建议后的文档性操作草案，系统不会真实下单。';
        COMMENT ON COLUMN order_drafts.order_draft_id IS '订单草案 ID，例如 draft:owner:asset:timestamp。';
        COMMENT ON COLUMN order_drafts.owner_id IS '用户或租户 ID。';
        COMMENT ON COLUMN order_drafts.portfolio_id IS '组合 ID。';
        COMMENT ON COLUMN order_drafts.asset_id IS '资产 ID。';
        COMMENT ON COLUMN order_drafts.market IS '市场标识，例如 ashare、fund、crypto_spot。';
        COMMENT ON COLUMN order_drafts.decision_log_id IS '来源决策日志 ID，草案必须能追溯到建议。';
        COMMENT ON COLUMN order_drafts.action IS '建议操作：buy、sell、add、reduce。';
        COMMENT ON COLUMN order_drafts.suggested_price_range IS '建议价格区间和依据快照。';
        COMMENT ON COLUMN order_drafts.suggested_position_ratio IS '建议仓位比例，可为空。';
        COMMENT ON COLUMN order_drafts.constraints IS '风险约束快照，例如止损、集中度和复核结论。';
        COMMENT ON COLUMN order_drafts.status IS '草案状态：drafted、superseded、cancelled。';
        COMMENT ON COLUMN order_drafts.disclaimer IS '非投资建议免责声明，必须非空。';
        COMMENT ON COLUMN order_drafts.created_at IS '创建时间。';
        COMMENT ON COLUMN order_drafts.updated_at IS '最近更新时间。';

        COMMENT ON TABLE execution_records IS
        '用户外部执行登记表，仅记录用户自行在交易软件完成后的结果。';
        COMMENT ON COLUMN execution_records.execution_id IS '执行登记 ID。';
        COMMENT ON COLUMN execution_records.owner_id IS '用户或租户 ID。';
        COMMENT ON COLUMN execution_records.portfolio_id IS '组合 ID。';
        COMMENT ON COLUMN execution_records.asset_id IS '资产 ID。';
        COMMENT ON COLUMN execution_records.market IS '市场标识。';
        COMMENT ON COLUMN execution_records.order_draft_id IS '关联订单草案 ID，可为空。';
        COMMENT ON COLUMN execution_records.decision_log_id IS '关联决策日志 ID，可为空。';
        COMMENT ON COLUMN execution_records.action IS '用户实际执行动作：buy、sell、add、reduce。';
        COMMENT ON COLUMN execution_records.executed_price IS '用户填报的执行价格。';
        COMMENT ON COLUMN execution_records.executed_quantity IS '用户填报的执行数量。';
        COMMENT ON COLUMN execution_records.executed_at IS '用户填报的执行时间。';
        COMMENT ON COLUMN execution_records.fee IS '交易费用，可为空。';
        COMMENT ON COLUMN execution_records.note IS '用户备注。';
        COMMENT ON COLUMN execution_records.source IS '执行来源，当前唯一合法值为 user_reported。';
        COMMENT ON COLUMN execution_records.created_at IS '登记入库时间。';
        """
    )


def downgrade() -> None:
    op.drop_index("idx_execution_records_decision", table_name="execution_records")
    op.drop_index("idx_execution_records_draft", table_name="execution_records")
    op.drop_index("idx_execution_records_asset_executed", table_name="execution_records")
    op.drop_index("idx_execution_records_owner_created", table_name="execution_records")
    op.drop_table("execution_records")
    op.drop_index("idx_order_drafts_asset_created", table_name="order_drafts")
    op.drop_index("idx_order_drafts_decision_status", table_name="order_drafts")
    op.drop_index("idx_order_drafts_owner_status", table_name="order_drafts")
    op.drop_table("order_drafts")
