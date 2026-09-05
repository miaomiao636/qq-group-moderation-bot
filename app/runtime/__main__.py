"""`uv run python -m app.runtime`：影子模式常驻运行器入口。"""

from __future__ import annotations

import asyncio

from app.runtime.runner import run

if __name__ == "__main__":
    asyncio.run(run())
