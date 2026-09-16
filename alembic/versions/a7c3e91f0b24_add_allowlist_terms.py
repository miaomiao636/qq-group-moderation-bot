"""add allowlist_terms

Revision ID: a7c3e91f0b24
Revises: af66bc20f1ed
Create Date: 2026-09-16 15:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e91f0b24"
down_revision: str | None = "af66bc20f1ed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "allowlist_terms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("term", sa.String(length=64), nullable=False, unique=True),
        sa.Column("normalized", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_allowlist_terms_normalized", "allowlist_terms", ["normalized"])


def downgrade() -> None:
    op.drop_index("ix_allowlist_terms_normalized", table_name="allowlist_terms")
    op.drop_table("allowlist_terms")
