"""admin audit for csrf-protected mutations

Revision ID: 2c8a41d9f0b7
Revises: 15f0e4b7a901
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c8a41d9f0b7"
down_revision: str | None = "15f0e4b7a901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_audits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_audits_created_at", "admin_audits", ["created_at"])
    op.create_index("ix_admin_audits_operator", "admin_audits", ["operator"])
    op.create_index("ix_admin_audits_action", "admin_audits", ["action"])


def downgrade() -> None:
    op.drop_index("ix_admin_audits_action", table_name="admin_audits")
    op.drop_index("ix_admin_audits_operator", table_name="admin_audits")
    op.drop_index("ix_admin_audits_created_at", table_name="admin_audits")
    op.drop_table("admin_audits")
