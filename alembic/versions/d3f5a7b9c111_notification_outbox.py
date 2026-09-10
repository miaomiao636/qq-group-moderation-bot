"""R-106 additive notification notices, delivery outbox and collector watermarks.

Revision ID: d3f5a7b9c111
Revises: c2e4f6a8b010

Downgrade removes notification data only; back up first if delivery history is
needed. No moderation, approval, action or legacy settings are changed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f5a7b9c111"
down_revision: str | None = "c2e4f6a8b010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_notices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_key"),
        sa.CheckConstraint("severity IN ('page', 'ticket')", name="ck_notification_severity"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_notification_notices_created_at", "notification_notices", ["created_at"])
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "notice_id",
            sa.Integer(),
            sa.ForeignKey("notification_notices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False, server_default="primary"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=False, server_default=""),
        sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "notice_id", "channel", "audience", name="uq_notification_delivery_target"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SENDING','SENT','FAILED','UNKNOWN','SKIPPED')",
            name="ck_notification_delivery_status",
        ),
        sa.CheckConstraint(
            "audience IN ('primary','backup')", name="ck_notification_delivery_audience"
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_notification_delivery_attempts"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_notification_deliveries_due", "notification_deliveries", ["status", "next_attempt_at"]
    )
    op.create_table(
        "notification_state",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("notification_deliveries")
    op.drop_table("notification_notices")
    op.drop_table("notification_state")
