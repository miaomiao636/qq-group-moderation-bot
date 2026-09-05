"""t102 event dedup and action audit tables

Revision ID: c7d2e8f91a03
Revises: 3a9c0c662c2e
Create Date: 2026-09-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d2e8f91a03"
down_revision: str | None = "3a9c0c662c2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_events",
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_table(
        "action_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("group_openid", sa.String(length=64), nullable=False),
        sa.Column("target_member_openid", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("err_code", sa.Integer(), nullable=True),
        sa.Column("err_message", sa.String(length=500), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("action_logs")
    op.drop_table("processed_events")
