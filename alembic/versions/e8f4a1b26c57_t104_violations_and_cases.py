"""t104 violations and cases tables

Revision ID: e8f4a1b26c57
Revises: c7d2e8f91a03
Create Date: 2026-09-05 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8f4a1b26c57"
down_revision: str | None = "c7d2e8f91a03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "violation_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_openid", sa.String(length=64), nullable=False),
        sa.Column("member_openid", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("rule_hits_json", sa.Text(), nullable=False),
        sa.Column("message_snapshot_json", sa.Text(), nullable=False),
        sa.Column("action_result_json", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Integer(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("revoke_reason", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_violation_records_group_openid", "violation_records", ["group_openid"])
    op.create_index("ix_violation_records_member_openid", "violation_records", ["member_openid"])
    op.create_index("ix_violation_records_case_id", "violation_records", ["case_id"])
    op.create_index("ix_violation_records_created_at", "violation_records", ["created_at"])
    op.create_table(
        "cases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("case_no", sa.String(length=32), nullable=False),
        sa.Column("group_openid", sa.String(length=64), nullable=False),
        sa.Column("member_openid", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("violation_ids_json", sa.Text(), nullable=False),
        sa.Column("audit_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_no"),
    )
    op.create_index("ix_cases_group_openid", "cases", ["group_openid"])
    op.create_index("ix_cases_member_openid", "cases", ["member_openid"])
    op.create_index("ix_cases_status", "cases", ["status"])


def downgrade() -> None:
    op.drop_index("ix_cases_status", table_name="cases")
    op.drop_index("ix_cases_member_openid", table_name="cases")
    op.drop_index("ix_cases_group_openid", table_name="cases")
    op.drop_table("cases")
    op.drop_index("ix_violation_records_created_at", table_name="violation_records")
    op.drop_index("ix_violation_records_case_id", table_name="violation_records")
    op.drop_index("ix_violation_records_member_openid", table_name="violation_records")
    op.drop_index("ix_violation_records_group_openid", table_name="violation_records")
    op.drop_table("violation_records")
