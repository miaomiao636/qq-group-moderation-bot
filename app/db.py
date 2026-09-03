"""数据库引擎与会话管理（SQLite WAL）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def _ensure_sqlite_dir(database_url: str) -> None:
    """确保 SQLite 数据库文件所在目录存在。"""
    if not database_url.startswith("sqlite"):
        return
    # 形如 sqlite+aiosqlite:///path/to/db
    path = database_url.split(":///", 1)[-1]
    if path and path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)


def _enable_sqlite_wal(engine: AsyncEngine) -> None:
    """为 SQLite 连接启用 WAL 模式，提升并发读写性能。"""

    @event.listens_for(engine.sync_engine, "connect")
    def _set_wal(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _make_engine() -> AsyncEngine:
    settings = get_settings()
    _ensure_sqlite_dir(settings.database_url)
    connect_args: dict[str, object] = {}
    if settings.database_url.startswith("sqlite"):
        connect_args = {"timeout": 30}
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    if settings.database_url.startswith("sqlite"):
        _enable_sqlite_wal(engine)
    return engine


engine = _make_engine()
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：提供数据库会话。"""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """创建所有表（脚手架阶段使用；正式迁移走 Alembic）。"""
    from app import models  # noqa: F401  确保模型已注册

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
