"""Add bounded recall confirmation evidence; do not backfill historical success."""

import sqlalchemy as sa
from alembic import op

revision = "f3c8a9d12064"
down_revision = "e1c7d4b8a902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recall_confirmations",
        sa.Column("intent_id", sa.Integer(), sa.ForeignKey("action_intents.id"), primary_key=True),
        *(
            sa.Column(name, sa.String(128), nullable=False)
            for name in ("account_id", "group_id", "user_id", "message_id", "source_event_key")
        ),
        sa.Column("source_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operator_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("notice_fingerprint", sa.String(64), nullable=True, unique=True),
    )
    op.create_index(
        "ix_recall_confirmation_match",
        "recall_confirmations",
        ["account_id", "group_id", "message_id"],
    )
    op.create_index(
        "ix_recall_confirmations_requested_at", "recall_confirmations", ["requested_at"]
    )


def downgrade() -> None:
    op.drop_table("recall_confirmations")
