"""t305 provider-aware group route key

``external_group_id`` 只在各自 provider 命名空间内有意义。将路由表从
单列主键改为 ``message_provider + external_group_id`` 联合主键，避免
QQ官方与OneBot中相同字符串的群身份冲突。

Revision ID: d4f7a9c2e601
Revises: b8e2f6a4c1d9
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f7a9c2e601"
down_revision: str | None = "b8e2f6a4c1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_route_table(name: str, *, provider_aware: bool) -> None:
    primary_key = (
        sa.PrimaryKeyConstraint("message_provider", "external_group_id")
        if provider_aware
        else sa.PrimaryKeyConstraint("external_group_id")
    )
    op.create_table(
        name,
        sa.Column("external_group_id", sa.String(length=128), nullable=False),
        sa.Column(
            "message_provider", sa.String(length=16), nullable=False, server_default="qq_official"
        ),
        sa.Column(
            "action_provider", sa.String(length=16), nullable=False, server_default="qq_official"
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        primary_key,
    )


def upgrade() -> None:
    _create_route_table("group_provider_routes_t305", provider_aware=True)
    op.execute(
        """
        INSERT INTO group_provider_routes_t305
            (external_group_id, message_provider, action_provider, updated_at)
        SELECT external_group_id, message_provider, action_provider, updated_at
        FROM group_provider_routes
        """
    )
    op.drop_table("group_provider_routes")
    op.rename_table("group_provider_routes_t305", "group_provider_routes")


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            """
            SELECT external_group_id
            FROM group_provider_routes
            GROUP BY external_group_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "无法降级路由表：同一 external_group_id 已存在多个 provider，请先人工合并路由"
        )
    _create_route_table("group_provider_routes_legacy", provider_aware=False)
    op.execute(
        """
        INSERT INTO group_provider_routes_legacy
            (external_group_id, message_provider, action_provider, updated_at)
        SELECT external_group_id, message_provider, action_provider, updated_at
        FROM group_provider_routes
        """
    )
    op.drop_table("group_provider_routes")
    op.rename_table("group_provider_routes_legacy", "group_provider_routes")
