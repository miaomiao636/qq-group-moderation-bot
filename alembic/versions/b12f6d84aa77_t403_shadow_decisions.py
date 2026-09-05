"""t403 shadow decisions table

Revision ID: b12f6d84aa77
Revises: e8f4a1b26c57
Create Date: 2026-09-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b12f6d84aa77"
down_revision: str | None = "e8f4a1b26c57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadow_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("group_openid", sa.String(length=64), nullable=False),
        sa.Column("member_openid", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("verdict", sa.String(length=24), nullable=False),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    op.create_index("ix_shadow_decisions_group_openid", "shadow_decisions", ["group_openid"])
    op.create_index("ix_shadow_decisions_member_openid", "shadow_decisions", ["member_openid"])
    op.create_index("ix_shadow_decisions_verdict", "shadow_decisions", ["verdict"])
    op.create_index("ix_shadow_decisions_created_at", "shadow_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_shadow_decisions_created_at", table_name="shadow_decisions")
    op.drop_index("ix_shadow_decisions_verdict", table_name="shadow_decisions")
    op.drop_index("ix_shadow_decisions_member_openid", table_name="shadow_decisions")
    op.drop_index("ix_shadow_decisions_group_openid", table_name="shadow_decisions")
    op.drop_table("shadow_decisions")
