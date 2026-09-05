"""group aliases

Revision ID: c6d9f2a51b34
Revises: a4c8e1f30d92
Create Date: 2026-09-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c6d9f2a51b34"
down_revision: str | None = "a4c8e1f30d92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_aliases",
        sa.Column("group_openid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("group_openid"),
    )


def downgrade() -> None:
    op.drop_table("group_aliases")
