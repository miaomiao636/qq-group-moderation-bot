"""t204 ai cache

Revision ID: 44de03b8a12f
Revises: 8ab12f3d9c60
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "44de03b8a12f"
down_revision: str | None = "8ab12f3d9c60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_cache",
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("cache_key"),
    )
    op.create_index("ix_ai_cache_model_id", "ai_cache", ["model_id"], unique=False)
    op.create_index("ix_ai_cache_prompt_version", "ai_cache", ["prompt_version"], unique=False)
    op.create_index("ix_ai_cache_expires_at", "ai_cache", ["expires_at"], unique=False)

    op.create_table(
        "ai_usage_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("group_openid", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error_kind", sa.String(length=64), nullable=False),
        sa.Column("cost_cents", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_usage_logs_group_openid", "ai_usage_logs", ["group_openid"], unique=False
    )
    op.create_index("ix_ai_usage_logs_message_id", "ai_usage_logs", ["message_id"], unique=False)
    op.create_index("ix_ai_usage_logs_cache_key", "ai_usage_logs", ["cache_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ai_usage_logs_cache_key", table_name="ai_usage_logs")
    op.drop_index("ix_ai_usage_logs_message_id", table_name="ai_usage_logs")
    op.drop_index("ix_ai_usage_logs_group_openid", table_name="ai_usage_logs")
    op.drop_table("ai_usage_logs")
    op.drop_index("ix_ai_cache_expires_at", table_name="ai_cache")
    op.drop_index("ix_ai_cache_prompt_version", table_name="ai_cache")
    op.drop_index("ix_ai_cache_model_id", table_name="ai_cache")
    op.drop_table("ai_cache")
