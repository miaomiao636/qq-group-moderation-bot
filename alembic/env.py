"""Alembic 迁移环境配置。"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from app import models  # noqa: F401  确保模型注册到 metadata
from app.actions import orchestrator  # noqa: F401
from app.config import get_settings
from app.db import Base
from app.moderation import (
    ai,  # noqa: F401
    dynamic_rules,  # noqa: F401
    feedback,  # noqa: F401
)
from app.notifications import models as notification_models  # noqa: F401
from app.runtime import inbox  # noqa: F401  持久接收箱模型
from sqlalchemy import event, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 从应用配置读取数据库 URL
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：不连接数据库，仅生成 SQL。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def _enable_sqlite_wal(engine: object) -> None:
    """为 SQLite 连接启用 WAL 模式，与运行时行为保持一致。"""

    @event.listens_for(engine.sync_engine, "connect")  # type: ignore[union-attr]
    def _set_wal(dbapi_conn: object, _: object) -> None:
        cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def run_async_migrations() -> None:
    """异步模式：连接数据库执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if get_settings().database_url.startswith("sqlite"):
        _enable_sqlite_wal(connectable)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
