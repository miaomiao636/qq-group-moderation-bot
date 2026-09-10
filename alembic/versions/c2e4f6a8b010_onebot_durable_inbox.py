"""R-105 durable OneBot inbox (additive, raw payload retention is enforced at runtime)."""

import sqlalchemy as sa
from alembic import op

revision = "c2e4f6a8b010"
down_revision = "b1d3f5a7c909"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onebot_inbox",
        sa.Column("event_key", sa.String(128), primary_key=True),
        sa.Column("self_id", sa.String(32), nullable=False),
        sa.Column("group_id", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_kind", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("status", "self_id", "group_id"):
        op.create_index(f"ix_onebot_inbox_{column}", "onebot_inbox", [column])


def downgrade() -> None:
    op.drop_table("onebot_inbox")
