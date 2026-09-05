"""r102 processed_events status

Revision ID: d2b1f9a60e45
Revises: c6d9f2a51b34
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2b1f9a60e45"
down_revision: str | None = "c6d9f2a51b34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processed_events",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PROCESSED"),
    )
    op.add_column(
        "processed_events",
        sa.Column("error_message", sa.String(length=500), nullable=False, server_default=""),
    )
    op.create_index("ix_processed_events_status", "processed_events", ["status"])


def downgrade() -> None:
    op.drop_index("ix_processed_events_status", table_name="processed_events")
    op.drop_column("processed_events", "error_message")
    op.drop_column("processed_events", "status")
