"""shadow sender_name

Revision ID: a4c8e1f30d92
Revises: f2a9c3d47b18
Create Date: 2026-09-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c8e1f30d92"
down_revision: str | None = "f2a9c3d47b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shadow_decisions",
        sa.Column("sender_name", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index("ix_shadow_decisions_sender_name", "shadow_decisions", ["sender_name"])


def downgrade() -> None:
    op.drop_index("ix_shadow_decisions_sender_name", table_name="shadow_decisions")
    op.drop_column("shadow_decisions", "sender_name")
