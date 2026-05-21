"""创建模型运行时配置表

Revision ID: 20260521_0009
Revises: 20260519_0008
Create Date: 2026-05-21 18:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260521_0009"
down_revision = "20260519_0008"
branch_labels = None
depends_on = None


def _jsonb_object(name: str = "payload") -> sa.Column:
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("provider_id", sa.String(length=160), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("provider_vendor", sa.String(length=64), nullable=False),
        sa.Column("provider_name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _jsonb_object(),
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
        sa.PrimaryKeyConstraint("provider_id"),
        sa.UniqueConstraint("provider_key", name="uq_model_providers_provider_key"),
    )
    op.create_index("idx_model_providers_vendor", "model_providers", ["provider_vendor"])
    op.create_index("idx_model_providers_enabled", "model_providers", ["is_enabled"])
    op.create_index("idx_model_providers_default", "model_providers", ["is_default"])

    op.create_table(
        "model_instances",
        sa.Column("model_instance_id", sa.String(length=160), nullable=False),
        sa.Column("provider_key", sa.String(length=64), nullable=False),
        sa.Column("model_key", sa.String(length=128), nullable=False),
        sa.Column("model_type", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=True),
        sa.Column("route_priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _jsonb_object(),
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
        sa.PrimaryKeyConstraint("model_instance_id"),
        sa.UniqueConstraint("model_key", name="uq_model_instances_model_key"),
    )
    op.create_index(
        "idx_model_instances_provider_enabled",
        "model_instances",
        ["provider_key", "is_enabled"],
    )
    op.create_index(
        "idx_model_instances_role_enabled",
        "model_instances",
        ["role", "is_enabled"],
    )
    op.create_index("idx_model_instances_model_type", "model_instances", ["model_type"])
    op.create_index("idx_model_instances_default", "model_instances", ["is_default"])

    op.create_table(
        "model_routing_rules",
        sa.Column("rule_id", sa.String(length=192), nullable=False),
        sa.Column("workflow_type", sa.String(length=64), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("model_key", sa.String(length=128), nullable=False),
        sa.Column(
            "decision_type",
            sa.String(length=64),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _jsonb_object(),
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
        sa.PrimaryKeyConstraint("rule_id"),
        sa.UniqueConstraint(
            "workflow_type",
            "task",
            "role",
            "decision_type",
            name="uq_model_routing_rules_scope",
        ),
    )
    op.create_index(
        "idx_model_routing_rules_workflow",
        "model_routing_rules",
        ["workflow_type", "task"],
    )
    op.create_index(
        "idx_model_routing_rules_role_enabled",
        "model_routing_rules",
        ["role", "is_enabled"],
    )

    op.create_table(
        "retrieval_profiles",
        sa.Column("profile_id", sa.String(length=160), nullable=False),
        sa.Column("profile_key", sa.String(length=128), nullable=False),
        sa.Column("profile_name", sa.String(length=255), nullable=False),
        sa.Column("usage_scope", sa.String(length=64), nullable=False),
        sa.Column("search_method", sa.String(length=32), nullable=False),
        sa.Column("embedding_model_key", sa.String(length=128), nullable=True),
        sa.Column("rerank_model_key", sa.String(length=128), nullable=True),
        sa.Column("top_k", sa.Integer(), server_default=sa.text("4"), nullable=False),
        sa.Column("score_threshold", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("reranking_enable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reranking_mode", sa.String(length=32), nullable=True),
        _jsonb_object("weights"),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        _jsonb_object(),
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
        sa.PrimaryKeyConstraint("profile_id"),
        sa.UniqueConstraint("profile_key", name="uq_retrieval_profiles_profile_key"),
    )
    op.create_index(
        "idx_retrieval_profiles_scope_default",
        "retrieval_profiles",
        ["usage_scope", "is_default"],
    )
    op.create_index("idx_retrieval_profiles_enabled", "retrieval_profiles", ["is_enabled"])


def downgrade() -> None:
    op.drop_table("retrieval_profiles")
    op.drop_table("model_routing_rules")
    op.drop_table("model_instances")
    op.drop_table("model_providers")
