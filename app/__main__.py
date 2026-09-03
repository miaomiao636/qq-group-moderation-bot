"""应用启动入口。

通过 `uv run python -m app` 启动，读取 `WEB_HOST` 与 `WEB_PORT` 配置，
确保 `.env` 中的端口设置实际生效。
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    """按配置启动 uvicorn 服务器。"""
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.web_host,
        port=settings.web_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
