"""FastAPI 应用入口。

脚手架阶段仅提供健康检查与基础路由；业务路由在后续任务中按职责挂载。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import init_db
from app.logging_config import setup_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：初始化日志与数据库。"""
    setup_logging()
    await init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="QQ 群多模态智能管理机器人",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        """健康检查端点。"""
        return {"status": "ok", "env": settings.app_env, "mode": settings.run_mode}

    return app


app = create_app()
