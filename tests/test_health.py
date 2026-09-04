"""健康检查、配置校验与启动入口测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


def test_healthz() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["env"] == "test"
    assert body["mode"] == "SAFE"


def test_healthz_requires_migrated_db(tmp_path: Path) -> None:
    """未迁移的数据库应被拒绝（Alembic 是唯一建表路径）。"""
    import asyncio

    from app.db import check_db_migrated
    from sqlalchemy.ext.asyncio import create_async_engine

    empty_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        with pytest.raises(RuntimeError, match="Alembic"):
            asyncio.run(check_db_migrated(empty_engine))
    finally:
        asyncio.run(empty_engine.dispose())


def test_healthz_rejects_stale_revision(tmp_path: Path) -> None:
    """数据库版本不等于当前代码 Alembic head 时应被拒绝。"""
    import asyncio

    from app.db import check_db_migrated
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
    try:
        # 手动创建 alembic_version 表并写入一个过期版本号
        async def _seed() -> None:
            async with engine.begin() as conn:
                await conn.execute(
                    text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
                )
                await conn.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES ('stale_revision')")
                )

        asyncio.run(_seed())
        with pytest.raises(RuntimeError, match="stale_revision"):
            asyncio.run(check_db_migrated(engine))
    finally:
        asyncio.run(engine.dispose())


def test_invalid_run_mode_rejected() -> None:
    """非法 RUN_MODE 应被配置校验拒绝。"""
    with pytest.raises(ValueError):
        Settings(run_mode="TYPO", _env_file=None)


def test_invalid_port_rejected() -> None:
    """越界端口应被配置校验拒绝。"""
    with pytest.raises(ValueError):
        Settings(web_port=99999, _env_file=None)


def test_invalid_retention_rejected() -> None:
    """非正保留天数应被配置校验拒绝。"""
    with pytest.raises(ValueError):
        Settings(raw_retention_days=0, _env_file=None)


def test_invalid_log_level_rejected() -> None:
    """非法日志级别应被配置校验拒绝。"""
    with pytest.raises(ValueError):
        Settings(log_level="TRACE", _env_file=None)


def test_prod_requires_admin_password() -> None:
    """生产环境必须设置非空管理员密码。"""
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        Settings(app_env="prod", admin_password="", _env_file=None)


def test_prod_rejects_whitespace_password() -> None:
    """生产环境仅含空白字符的密码应被拒绝。"""
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        Settings(app_env="prod", admin_password="   ", _env_file=None)


def test_prod_accepts_admin_password() -> None:
    """生产环境设置密码后应通过校验。"""
    s = Settings(app_env="prod", admin_password="strong-pass", _env_file=None)
    assert s.app_env == "prod"
    assert s.admin_password == "strong-pass"


def test_web_port_effective() -> None:
    """WEB_PORT 配置应被启动入口读取。"""
    # 验证 __main__ 使用配置中的端口（通过 monkeypatch 检查 uvicorn.run 参数）
    import uvicorn
    from app.__main__ import main

    captured: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(uvicorn, "run", fake_run)
    try:
        main()
    finally:
        monkeypatch.undo()
    assert captured["port"] == 8123
    assert captured["host"] == "127.0.0.1"
