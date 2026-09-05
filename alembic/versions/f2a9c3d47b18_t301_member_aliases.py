"""t301 member aliases

Revision ID: f2a9c3d47b18
Revises: b12f6d84aa77
Create Date: 2026-09-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a9c3d47b18"
down_revision: str | None = "b12f6d84aa77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "member_aliases",
        sa.Column("member_openid", sa.String(length=64), nullable=False),
        sa.Column("qq_number", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("member_openid"),
    )


def downgrade() -> None:
    op.drop_table("member_aliases")
