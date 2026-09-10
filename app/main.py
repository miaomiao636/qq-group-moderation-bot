"""FastAPI 应用入口。

脚手架阶段仅提供健康检查与基础路由；业务路由在后续任务中按职责挂载。
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import check_db_migrated
from app.logging_config import setup_logging
from app.web.routes import router as admin_router


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：初始化日志、校验迁移、清理僵尸租约。"""
    setup_logging()
    await check_db_migrated()
    # 启动清理：过期PROCESSING→FAILED，统计遗留PENDING
    from app.core.dedup import reap_stuck_leases
    from app.db import SessionLocal

    async with SessionLocal() as session:
        reaped, pending = await reap_stuck_leases(session)
    if reaped or pending:
        import logging

        logging.getLogger(__name__).warning(
            "启动清理：%d条过期租约已标记FAILED，%d条PENDING待重放", reaped, pending
        )
    from app.notifications.config import NotificationSettings
    from app.notifications.runtime import start_notification_runtime, stop_notification_runtime
    from app.runtime.onebot_ws import start_onebot_runtime, stop_onebot_runtime

    if get_settings().onebot_ws_enabled:
        await start_onebot_runtime()
    try:
        start_notification_runtime(NotificationSettings())
        yield
    finally:
        await stop_notification_runtime()
        if get_settings().onebot_ws_enabled:
            await stop_onebot_runtime()


def create_app() -> FastAPI:
    from app.notifications.config import NotificationSettings

    settings = get_settings()
    notifications = NotificationSettings()
    if (
        notifications.enabled
        and notifications.heartbeat_enabled
        and any(
            notifications.heartbeat_probe_token == credential
            for credential in (
                settings.admin_password,
                settings.agent_api_token,
                settings.agent_api_read_token,
                settings.onebot_access_token,
            )
            if credential
        )
    ):
        raise ValueError("heartbeat requires a dedicated credential")
    app = FastAPI(
        title="QQ 群多模态智能管理机器人",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, object]:
        """健康检查端点。

        T-306：启用 OneBot 反向WS时附带 ``onebot`` 就绪状态块
        （连接/登录态/心跳/队列积压，``ready``/``degraded``）——
        进程存活不代表系统就绪。
        """
        payload: dict[str, object] = {
            "status": "ok",
            "env": settings.app_env,
            "mode": settings.run_mode,
        }
        if settings.onebot_ws_enabled:
            from app.runtime.onebot_ws import onebot_status

            payload["onebot"] = onebot_status.snapshot(include_sensitive=False)
        return payload

    @app.get("/health/ready", include_in_schema=False)
    async def notification_readiness(request: Request) -> JSONResponse:
        if not notifications.enabled or not notifications.heartbeat_enabled:
            return JSONResponse({"status": "disabled"}, status_code=404)
        expected = "Bearer " + notifications.heartbeat_probe_token
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        from app.notifications.health import check_readiness

        payload = await check_readiness()
        return JSONResponse(payload, status_code=200 if payload["ready"] else 503)

    # 管理后台（T-301/T-302）：服务端页面，强制登录；仅绑定本机/可信内网
    app.include_router(admin_router)
    from app.web.notifications import router as notification_router

    app.include_router(notification_router)

    # NapCat OneBot 11 反向WS入站（T-306）：默认关闭；启用需令牌+本机/内网绑定
    if settings.onebot_ws_enabled:
        from app.runtime.onebot_ws import build_onebot_router

        app.include_router(build_onebot_router(settings.onebot_ws_path))

    return app


app = create_app()
