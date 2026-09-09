"""group_settings + system_settings tables

T-303 UX：按群管理开关（审核/动作）与系统级设置（保留期/清理）。

Revision ID: e1a4b8c2d3f5
Revises: f6a2c7e91b40
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1a4b8c2d3f5"
down_revision: str | None = "f6a2c7e91b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_settings",
        sa.Column("group_openid", sa.String(128), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, server_default=""),
        sa.Column("moderation_enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("action_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(255), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
    op.drop_table("group_settings")
