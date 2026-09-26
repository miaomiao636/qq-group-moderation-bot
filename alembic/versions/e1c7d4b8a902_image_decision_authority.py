"""A2 additive decision authority; data backfill remains an explicit offline step."""

import sqlalchemy as sa
from alembic import op

revision = "e1c7d4b8a902"
down_revision = "d4b7c1e9a502"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, size in (("decision_state", 16), ("decision_source", 96), ("decision_operator", 64)):
        op.add_column(
            "image_allowlist", sa.Column(name, sa.String(size), nullable=False, server_default="")
        )
    op.add_column(
        "image_allowlist",
        sa.Column("decision_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "image_allowlist", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "image_allowlist", sa.Column("history_json", sa.Text(), nullable=False, server_default="{}")
    )
    op.create_index("ix_image_allowlist_decision_state", "image_allowlist", ["decision_state"])


def downgrade() -> None:
    op.drop_index("ix_image_allowlist_decision_state", table_name="image_allowlist")
    with op.batch_alter_table("image_allowlist") as batch:
        for name in (
            "history_json",
            "decided_at",
            "decision_version",
            "decision_operator",
            "decision_source",
            "decision_state",
        ):
            batch.drop_column(name)
    op.execute("DELETE FROM system_settings WHERE key='image_decision_authority_version'")
