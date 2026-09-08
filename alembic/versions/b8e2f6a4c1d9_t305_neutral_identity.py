"""t305 neutral identity expand/migrate

T-305 传输中立身份：为既有表增加 provider / external_group_id /
external_user_id（/ external_message_id）中立列并回填历史数据
（全部来自官方通道，provider='qq_official'）；新增每群通道路由表
group_provider_routes。不删除、不改名任何旧列（contract 阶段另行评估）。

Revision ID: b8e2f6a4c1d9
Revises: a0b4d72e5f31
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.core.identity_backfill import NEUTRAL_BACKFILL_STATEMENTS

revision: str = "b8e2f6a4c1d9"
down_revision: str | None = "a0b4d72e5f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_identity_columns(
    table: str, *, with_provider: bool, with_message_id: bool = False
) -> None:
    with op.batch_alter_table(table) as batch:
        if with_provider:
            batch.add_column(
                sa.Column("provider", sa.String(16), nullable=False, server_default="qq_official")
            )
        batch.add_column(
            sa.Column("external_group_id", sa.String(128), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("external_user_id", sa.String(128), nullable=False, server_default="")
        )
        if with_message_id:
            batch.add_column(
                sa.Column("external_message_id", sa.String(128), nullable=False, server_default="")
            )


def _drop_identity_columns(
    table: str, *, with_provider: bool, with_message_id: bool = False
) -> None:
    with op.batch_alter_table(table) as batch:
        if with_message_id:
            batch.drop_column("external_message_id")
        batch.drop_column("external_user_id")
        batch.drop_column("external_group_id")
        if with_provider:
            batch.drop_column("provider")


def upgrade() -> None:
    _add_identity_columns("processed_events", with_provider=True)
    _add_identity_columns("shadow_decisions", with_provider=True)
    _add_identity_columns("violation_records", with_provider=True)
    _add_identity_columns("cases", with_provider=True)
    _add_identity_columns("action_intents", with_provider=True, with_message_id=True)
    _add_identity_columns("action_logs", with_provider=True, with_message_id=True)
    _add_identity_columns("feedback_records", with_provider=True)
    _add_identity_columns("ai_usage_logs", with_provider=False)

    for statement in NEUTRAL_BACKFILL_STATEMENTS:
        op.execute(statement)

    op.create_table(
        "group_provider_routes",
        sa.Column("external_group_id", sa.String(length=128), nullable=False),
        sa.Column(
            "message_provider", sa.String(length=16), nullable=False, server_default="qq_official"
        ),
        sa.Column(
            "action_provider", sa.String(length=16), nullable=False, server_default="qq_official"
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("external_group_id"),
    )


def downgrade() -> None:
    op.drop_table("group_provider_routes")
    _drop_identity_columns("ai_usage_logs", with_provider=False)
    _drop_identity_columns("feedback_records", with_provider=True)
    _drop_identity_columns("action_logs", with_provider=True, with_message_id=True)
    _drop_identity_columns("action_intents", with_provider=True, with_message_id=True)
    _drop_identity_columns("cases", with_provider=True)
    _drop_identity_columns("violation_records", with_provider=True)
    _drop_identity_columns("shadow_decisions", with_provider=True)
    _drop_identity_columns("processed_events", with_provider=True)
