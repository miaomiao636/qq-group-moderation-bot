"""add allowlist_members（成员白名单，负责人 2026-09-18）

新增 ``allowlist_members`` 表：按 ``provider + external_user_id`` 精确匹配的成员
白名单。NapCat 主通道的 ``external_user_id`` 即 OneBot 数字 QQ 号；官方通道为
openid，两通道隔离，不跨通道匹配。

设计要点（与 ``allowlist_terms`` 保持一致，便于运维与审计）：
- ``INTEGER PRIMARY KEY AUTOINCREMENT``：删除后 ID 不复用，旧表单不能操作替代对象；
- ``(provider, external_user_id)`` 唯一约束：并发写入无法绕过去重；
- 仅新增表，不改动既有表结构，可完整 downgrade。

Revision ID: c9a1f4d27e30
Revises: b8d4f2a05e31
Create Date: 2026-09-18 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9a1f4d27e30"
down_revision: str | None = "b8d4f2a05e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "allowlist_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(length=16), nullable=False, server_default="onebot"),
        sa.Column("external_user_id", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "external_user_id", name="uq_allowlist_member_identity"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("allowlist_members")
