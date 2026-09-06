"""t105 dynamic rules

Revision ID: 8ab12f3d9c60
Revises: 2c8a41d9f0b7
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8ab12f3d9c60"
down_revision: str | None = "2c8a41d9f0b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rule_sets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_sets_scope", "rule_sets", ["scope"])
    op.create_index("ix_rule_sets_scope_key", "rule_sets", ["scope_key"])

    op.create_table(
        "rule_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_set_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["rule_set_id"], ["rule_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_versions_rule_set_id", "rule_versions", ["rule_set_id"])
    op.create_index("ix_rule_versions_status", "rule_versions", ["status"])

    op.create_table(
        "rule_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=False),
        sa.Column("pattern", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["rule_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_items_version_id", "rule_items", ["version_id"])

    op.create_table(
        "rule_audits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_set_id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["rule_set_id"], ["rule_sets.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["rule_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_audits_rule_set_id", "rule_audits", ["rule_set_id"])


def downgrade() -> None:
    op.drop_index("ix_rule_audits_rule_set_id", table_name="rule_audits")
    op.drop_table("rule_audits")
    op.drop_index("ix_rule_items_version_id", table_name="rule_items")
    op.drop_table("rule_items")
    op.drop_index("ix_rule_versions_status", table_name="rule_versions")
    op.drop_index("ix_rule_versions_rule_set_id", table_name="rule_versions")
    op.drop_table("rule_versions")
    op.drop_index("ix_rule_sets_scope_key", table_name="rule_sets")
    op.drop_index("ix_rule_sets_scope", table_name="rule_sets")
    op.drop_table("rule_sets")
