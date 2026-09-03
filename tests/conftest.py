"""pytest 共享夹具。

测试数据库使用系统临时目录隔离，避免固定路径在本地残留或并发测试间互相影响。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 在导入 app 之前设置测试环境变量，确保 app.config 与 app.db 使用临时数据库。
# 使用系统临时目录，测试结束后由操作系统清理，不污染仓库。
_TMP_DIR = tempfile.mkdtemp(prefix="qqbot-test-")
_TEST_DB = Path(_TMP_DIR) / "test.db"

os.environ["APP_ENV"] = "test"
os.environ["RUN_MODE"] = "SAFE"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_DB}"
os.environ["WEB_HOST"] = "127.0.0.1"
os.environ["WEB_PORT"] = "8123"
os.environ["LOG_LEVEL"] = "INFO"


@pytest.fixture(scope="session", autouse=True)
def _migrated_db() -> None:
    """在测试会话开始时对临时数据库执行 Alembic 迁移。

    应用启动会校验数据库已通过 Alembic 迁移，因此测试前必须先迁移。
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
