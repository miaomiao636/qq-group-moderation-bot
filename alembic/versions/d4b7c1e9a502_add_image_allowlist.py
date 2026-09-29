"""add image_allowlist（图片感知哈希白名单，负责人 2026-09-19）

仅新增 ``image_allowlist`` 表：
- ``phash``（64 位 dHash 的 16 位十六进制）**唯一**：重复导入不会产生重复行；
- ``enabled`` 控制是否参与命中；``hit_count`` best-effort 记录命中次数；
- 不改动既有表结构，可完整 downgrade（drop table）。

Revision ID: d4b7c1e9a502
Revises: c9a1f4d27e30
Create Date: 2026-09-19 12:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4b7c1e9a502"
down_revision: str | None = "c9a1f4d27e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "image_allowlist",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("phash", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.UniqueConstraint("phash", name="uq_image_allowlist_phash"),
    )


def downgrade() -> None:
    op.drop_table("image_allowlist")
