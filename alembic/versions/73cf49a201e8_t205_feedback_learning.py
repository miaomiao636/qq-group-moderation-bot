"""t205 feedback learning

Revision ID: 73cf49a201e8
Revises: 44de03b8a12f
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "73cf49a201e8"
down_revision: str | None = "44de03b8a12f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("group_openid", sa.String(length=128), nullable=False),
        sa.Column("member_openid", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("sample_text_masked", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_feedback_records_message_id", "feedback_records", ["message_id"], unique=False
    )
    op.create_index(
        "ix_feedback_records_group_openid", "feedback_records", ["group_openid"], unique=False
    )
    op.create_index(
        "ix_feedback_records_member_openid", "feedback_records", ["member_openid"], unique=False
    )
    op.create_index("ix_feedback_records_label", "feedback_records", ["label"], unique=False)

    op.create_table(
        "rule_candidates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=False),
        sa.Column("pattern", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("support_count", sa.Integer(), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("replay_report_json", sa.Text(), nullable=False),
        sa.Column("generated_by", sa.String(length=64), nullable=False),
        sa.Column("copied_version_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_candidates_scope_key", "rule_candidates", ["scope_key"], unique=False)
    op.create_index("ix_rule_candidates_status", "rule_candidates", ["status"], unique=False)

    op.create_table(
        "rule_candidate_examples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("feedback_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("member_openid", sa.String(length=128), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rule_candidate_examples_candidate_id",
        "rule_candidate_examples",
        ["candidate_id"],
        unique=False,
    )
    op.create_index(
        "ix_rule_candidate_examples_feedback_id",
        "rule_candidate_examples",
        ["feedback_id"],
        unique=False,
    )
    op.create_index(
        "ix_rule_candidate_examples_message_id",
        "rule_candidate_examples",
        ["message_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_rule_candidate_examples_message_id", table_name="rule_candidate_examples")
    op.drop_index("ix_rule_candidate_examples_feedback_id", table_name="rule_candidate_examples")
    op.drop_index("ix_rule_candidate_examples_candidate_id", table_name="rule_candidate_examples")
    op.drop_table("rule_candidate_examples")
    op.drop_index("ix_rule_candidates_status", table_name="rule_candidates")
    op.drop_index("ix_rule_candidates_scope_key", table_name="rule_candidates")
    op.drop_table("rule_candidates")
    op.drop_index("ix_feedback_records_label", table_name="feedback_records")
    op.drop_index("ix_feedback_records_member_openid", table_name="feedback_records")
    op.drop_index("ix_feedback_records_group_openid", table_name="feedback_records")
    op.drop_index("ix_feedback_records_message_id", table_name="feedback_records")
    op.drop_table("feedback_records")
