"""T-306 shadow decision external message identity.

Revision ID: f6a2c7e91b40
Revises: d4f7a9c2e601
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a2c7e91b40"
down_revision: str | None = "d4f7a9c2e601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shadow_decisions") as batch:
        batch.add_column(
            sa.Column("external_message_id", sa.String(128), nullable=False, server_default="")
        )
    op.execute(
        sa.text(
            "UPDATE shadow_decisions SET external_message_id = message_id "
            "WHERE external_message_id = ''"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("shadow_decisions") as batch:
        batch.drop_column("external_message_id")
