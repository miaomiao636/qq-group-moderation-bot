"""allowlist: no id reuse (AUTOINCREMENT) + normalized unique

R-115 W03/W04 整改：
- W03：重建 allowlist_terms 为 ``INTEGER PRIMARY KEY AUTOINCREMENT``——SQLite 删除后
  ID 不复用，旧页面（指向旧 id）的删除/启停表单不能再操作替代对象（原实现是
  普通 INTEGER PRIMARY KEY，删除最大 id 后新词会复用同一 id）。
- W04：``normalized`` 由普通索引升级为**唯一约束**——并发写入大小写/变体等价词
  无法再绕过去重（原实现仅对原始 term 唯一）。

迁移前核查：现存重复 normalized 必须为 0；发现重复时**中止迁移并报错**
（不静默取更宽松值，由人工给出合并策略后再迁移）。

Revision ID: b8d4f2a05e31
Revises: a7c3e91f0b24
Create Date: 2026-09-16 19:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d4f2a05e31"
down_revision: str | None = "a7c3e91f0b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_TABLE_SQL = """
CREATE TABLE allowlist_terms (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    term VARCHAR(64) NOT NULL,
    normalized VARCHAR(64) NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_by VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE (term),
    UNIQUE (normalized)
)
"""

_OLD_TABLE_SQL = """
CREATE TABLE allowlist_terms (
    id INTEGER NOT NULL,
    term VARCHAR(64) NOT NULL,
    normalized VARCHAR(64) NOT NULL,
    enabled BOOLEAN NOT NULL,
    created_by VARCHAR(64) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (term)
)
"""


def _assert_no_duplicate_normalized() -> None:
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT normalized, COUNT(*) AS c FROM allowlist_terms "
                "GROUP BY normalized HAVING c > 1"
            )
        )
        .fetchall()
    )
    if rows:
        raise RuntimeError(
            f"存在重复归一化白名单词，需人工合并后再迁移（不静默取更宽松值）: {rows}"
        )


def upgrade() -> None:
    _assert_no_duplicate_normalized()
    # SQLite 无法直接修改主键语义：rename → 重建 → 复制 → 删除旧表。
    op.execute("ALTER TABLE allowlist_terms RENAME TO _allowlist_terms_old")
    op.execute(_NEW_TABLE_SQL)
    op.execute(
        "INSERT INTO allowlist_terms "
        "(id, term, normalized, enabled, created_by, created_at, updated_at) "
        "SELECT id, term, normalized, enabled, created_by, created_at, updated_at "
        "FROM _allowlist_terms_old"
    )
    op.execute("DROP TABLE _allowlist_terms_old")


def downgrade() -> None:
    op.execute("ALTER TABLE allowlist_terms RENAME TO _allowlist_terms_new")
    op.execute(_OLD_TABLE_SQL)
    op.execute(
        "INSERT INTO allowlist_terms "
        "(id, term, normalized, enabled, created_by, created_at, updated_at) "
        "SELECT id, term, normalized, enabled, created_by, created_at, updated_at "
        "FROM _allowlist_terms_new"
    )
    op.execute("DROP TABLE _allowlist_terms_new")
    op.create_index("ix_allowlist_terms_normalized", "allowlist_terms", ["normalized"])
