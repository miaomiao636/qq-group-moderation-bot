"""SQLite 相对路径规范化回归测试（R-101 复验项9/10）。

覆盖：
- README 默认配置 `sqlite+aiosqlite:///./data/moderation.db` 稳定解析到项目数据目录。
- 从非项目工作目录启动时，相对路径仍解析到同一绝对路径，不依赖当前工作目录。
- Windows 绝对路径格式（`C:/...` 与 `C:\\...`）保持不变。
- 绝对路径与 `:memory:` 保持不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import PROJECT_ROOT, _normalize_sqlite_url


def test_readme_default_relative_path_resolves_to_project_root() -> None:
    """README 默认相对路径应解析到项目根目录下的 data/moderation.db。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:///./data/moderation.db")
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert url == expected
    # 解析结果必须是绝对路径，且位于项目根目录下
    assert url.startswith("sqlite+aiosqlite:///")
    assert str(PROJECT_ROOT) in url


def test_relative_path_without_dot_slash() -> None:
    """不含 `./` 的相对路径也应解析到项目根目录。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:///data/moderation.db")
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert url == expected


def test_relative_path_resolves_same_regardless_of_cwd(monkeypatch) -> None:
    """从不同工作目录解析相对路径，结果必须一致（不依赖当前工作目录）。"""
    import tempfile

    from app.config import Settings

    # 模拟从非项目目录启动：切换工作目录到系统临时目录
    other_cwd = Path(tempfile.mkdtemp(prefix="qqbot-cwd-"))
    monkeypatch.chdir(other_cwd)

    s = Settings(database_url="sqlite+aiosqlite:///./data/moderation.db", _env_file=None)
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert s.database_url == expected


def test_absolute_unix_path_preserved() -> None:
    """Unix 绝对路径应保持不变。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:////var/lib/qqbot/moderation.db")
    assert url == "sqlite+aiosqlite:////var/lib/qqbot/moderation.db"


def test_windows_absolute_path_preserved() -> None:
    """Windows 绝对路径（正斜杠）应保持不变。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:///C:/data/moderation.db")
    assert url == "sqlite+aiosqlite:///C:/data/moderation.db"


def test_windows_absolute_path_backslash_preserved() -> None:
    """Windows 绝对路径（反斜杠）应保持不变。"""
    url = _normalize_sqlite_url(r"sqlite+aiosqlite:///C:\data\moderation.db")
    assert url == r"sqlite+aiosqlite:///C:\data\moderation.db"


def test_memory_database_preserved() -> None:
    """内存数据库 `:memory:` 应保持不变。"""
    url = _normalize_sqlite_url("sqlite+aiosqlite:///:memory:")
    assert url == "sqlite+aiosqlite:///:memory:"


def test_non_sqlite_url_preserved() -> None:
    """非 SQLite URL（如 PostgreSQL）应保持不变。"""
    url = _normalize_sqlite_url("postgresql+asyncpg://user:pass@host/db")
    assert url == "postgresql+asyncpg://user:pass@host/db"


def test_settings_normalizes_relative_url() -> None:
    """Settings 加载时应自动规范化相对 SQLite 路径。"""
    from app.config import Settings

    s = Settings(database_url="sqlite+aiosqlite:///./data/moderation.db", _env_file=None)
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert s.database_url == expected


def test_migration_uses_normalized_absolute_path(monkeypatch, tmp_path: Path) -> None:
    """迁移使用的数据库 URL 必须是规范化后的绝对路径。

    这是端到端回归：`alembic/env.py` 通过 `get_settings().database_url` 读取
    数据库地址，而该地址在配置层已被 `_normalize_sqlite_url` 规范化为基于
    项目根目录的绝对路径。因此无论从哪个工作目录执行迁移，都会连接同一数据库，
    不会在非项目目录下创建漂移的空库。
    """
    import asyncio

    from app.config import Settings
    from app.db import check_db_migrated
    from sqlalchemy.ext.asyncio import create_async_engine

    # 切换到非项目目录，模拟从项目外启动
    monkeypatch.chdir(tmp_path)

    # 使用 README 默认相对路径构造配置，验证其被规范化为项目根目录下的绝对路径
    s = Settings(database_url="sqlite+aiosqlite:///./data/moderation.db", _env_file=None)
    normalized = s.database_url
    expected = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'moderation.db'}"
    assert normalized == expected

    # 规范化后的路径必须指向项目根目录，而非当前工作目录（tmp_path）
    assert str(PROJECT_ROOT) in normalized
    assert str(tmp_path) not in normalized

    # 用规范化后的 URL 创建引擎，验证其指向项目数据目录下的真实文件
    db_path = PROJECT_ROOT / "data" / "moderation.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    try:
        engine = create_async_engine(normalized)
        try:
            # 未迁移时拒绝启动（Alembic 是唯一建表路径）
            with pytest.raises(RuntimeError, match="Alembic"):
                asyncio.run(check_db_migrated(engine))
        finally:
            asyncio.run(engine.dispose())
        # 连接后数据库文件应创建在项目数据目录下
        assert db_path.exists(), "数据库文件应位于项目数据目录"
        # 非项目目录下不应出现漂移的空库
        assert not (tmp_path / "data" / "moderation.db").exists()
    finally:
        if db_path.exists():
            db_path.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
