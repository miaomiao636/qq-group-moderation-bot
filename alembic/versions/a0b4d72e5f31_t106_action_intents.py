"""t106 action intents

Revision ID: a0b4d72e5f31
Revises: 73cf49a201e8
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a0b4d72e5f31"
down_revision: str | None = "73cf49a201e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_intents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("group_openid", sa.String(length=128), nullable=False),
        sa.Column("target_member_openid", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_action_intents_idempotency_key",
        "action_intents",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index("ix_action_intents_status", "action_intents", ["status"], unique=False)
    op.create_index(
        "ix_action_intents_group_openid", "action_intents", ["group_openid"], unique=False
    )
    op.create_index("ix_action_intents_message_id", "action_intents", ["message_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_action_intents_message_id", table_name="action_intents")
    op.drop_index("ix_action_intents_group_openid", table_name="action_intents")
    op.drop_index("ix_action_intents_status", table_name="action_intents")
    op.drop_index("ix_action_intents_idempotency_key", table_name="action_intents")
    op.drop_table("action_intents")
