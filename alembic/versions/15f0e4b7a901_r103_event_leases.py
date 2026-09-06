"""r103 event processing leases

Revision ID: 15f0e4b7a901
Revises: d2b1f9a60e45
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "15f0e4b7a901"
down_revision: str | None = "d2b1f9a60e45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processed_events",
        sa.Column("error_kind", sa.String(length=24), nullable=False, server_default=""),
    )
    op.add_column(
        "processed_events",
        sa.Column("lease_token", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "processed_events",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processed_events",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "processed_events",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_processed_events_lease_token", "processed_events", ["lease_token"])


def downgrade() -> None:
    op.drop_index("ix_processed_events_lease_token", table_name="processed_events")
    op.drop_column("processed_events", "next_retry_at")
    op.drop_column("processed_events", "attempts")
    op.drop_column("processed_events", "lease_expires_at")
    op.drop_column("processed_events", "lease_token")
    op.drop_column("processed_events", "error_kind")
