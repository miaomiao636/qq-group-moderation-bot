"""pytest 共享夹具。"""

from __future__ import annotations

import os
from pathlib import Path

# 测试环境使用独立临时数据库，避免污染本地数据
_TEST_DB = Path(__file__).resolve().parent / "test_data" / "test.db"
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB}")
os.environ.setdefault("RUN_MODE", "SAFE")
