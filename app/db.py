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


def get_head_revision() -> str:
    """返回当前代码中 Alembic 迁移链的最新版本（head）。

    从 `alembic/versions/` 脚本目录解析，确保与代码中的迁移定义一致。
    `alembic.ini` 路径基于项目根目录解析，而非当前工作目录，
    因此无论从哪个目录启动应用都能正确定位迁移脚本。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.config import PROJECT_ROOT

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    # `alembic.ini` 中 `script_location = alembic` 是相对路径，
    # 会基于当前工作目录解析。这里改为基于项目根目录的绝对路径，
    # 确保从任意工作目录启动都能定位迁移脚本。
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("未找到任何 Alembic 迁移脚本，无法确定 head 版本。")
    return head


async def get_db_revision(
    target_engine: AsyncEngine | None = None,
) -> str | None:
    """返回数据库的 Alembic 版本号；`alembic_version` 表不存在时返回 None。"""
    from sqlalchemy import text

    conn_engine = target_engine or engine
    try:
        async with conn_engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
    except Exception:  # noqa: BLE001 - 迁移未执行时表不存在
        return None
    return row[0] if row else None


async def check_db_migrated(
    target_engine: AsyncEngine | None = None,
) -> None:
    """校验数据库已迁移到当前代码的 Alembic 最新版本（head）。

    必须存在 `alembic_version` 表，且版本号等于当前代码的 head 版本，
    否则拒绝启动。仅检查"版本非空"不足以防止旧数据库结构直接运行新代码。
    """
    revision = await get_db_revision(target_engine)
    if revision is None:
        raise RuntimeError(
            "数据库未通过 Alembic 迁移（缺少 alembic_version 记录）。"
            "请先运行 `uv run alembic upgrade head`，禁止使用 create_all 绕过迁移。"
        )
    head = get_head_revision()
    if revision != head:
        raise RuntimeError(
            f"数据库迁移版本 {revision!r} 与当前代码要求的 Alembic head {head!r} 不一致。"
            "请运行 `uv run alembic upgrade head` 将数据库升级到最新版本。"
        )
