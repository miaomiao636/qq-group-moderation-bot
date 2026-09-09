"""NapCat OneBot 11 反向WebSocket入站与影子运行器（T-306）。

安全边界：
- 端点随管理后台同进程监听 ``WEB_HOST``（配置校验强制本机回环或可信内网）；
- 连接必须在 ``Authorization: Bearer`` 请求头携带访问令牌，
  常量时间比较，失败一律拒绝（fail-closed）；
- 影子模式硬约束：本模块**不实现也绝不调用**任何 OneBot 管理动作
  （撤回/禁言/警告/踢人属 T-307，默认影子关闭）；所有判定只落库。

就绪语义（不等于Python进程存活）：
- ``ready``：已连接 NapCat、QQ登录态在线、心跳新鲜、队列积压未满；
- ``degraded``：未连接、未登录、心跳超时或队列积压——任一即降级。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.db import SessionLocal
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.rules import TextRuleEngine
from app.runtime import onebot_wiring

logger = logging.getLogger(__name__)

# 连续非法帧上限：超过即断开连接（防恶意客户端刷屏）
_MAX_INVALID_STREAK = 50
# 每群最后事件时间最多保留条目数（防内存无界增长）
_MAX_TRACKED_GROUPS = 200


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class OneBotRuntimeStatus:
    """NapCat 连接/QQ登录态/心跳/每群事件/队列积压 的就绪状态注册表。"""

    def __init__(self) -> None:
        self.enabled = False
        self.connected = False
        self.login_state = "unknown"  # unknown | online | offline
        self.self_id = ""
        self.connect_count = 0
        self.disconnect_count = 0
        self.last_connect_at: str | None = None
        self.last_disconnect_at: str | None = None
        self.last_heartbeat_at: str | None = None
        self._last_heartbeat_mono: float | None = None
        self.last_event_at: str | None = None
        self.group_last_event: dict[str, str] = {}
        self.processed_total = 0
        self.skipped_total = 0
        self.failed_total = 0
        self.invalid_total = 0
        self.ignored_total = 0
        self.last_error = ""
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._queue_max = 0
        self._heartbeat_timeout = 90.0

    def configure(self, *, queue_max: int, heartbeat_timeout: float) -> None:
        self._queue_max = queue_max
        self._heartbeat_timeout = heartbeat_timeout
        self.enabled = True

    def bind_queue(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queue = queue

    # ---- 连接生命周期 ----

    def register_connect(self) -> None:
        self.connected = True
        self.connect_count += 1
        self.last_connect_at = _utcnow_iso()

    def register_disconnect(self, error: str = "") -> None:
        self.connected = False
        self.disconnect_count += 1
        self.last_disconnect_at = _utcnow_iso()
        self.login_state = "offline"
        if error:
            self.last_error = error[:200]

    def mark_online(self, self_id: str) -> None:
        """收到 lifecycle connect：NapCat 已登录 QQ。"""
        self.login_state = "online"
        if self_id:
            self.self_id = self_id

    def mark_heartbeat(self, status: Any = None) -> None:
        self.last_heartbeat_at = _utcnow_iso()
        self._last_heartbeat_mono = time.monotonic()
        if isinstance(status, dict):
            if status.get("online") is False or status.get("good") is False:
                self.login_state = "offline"
            elif status.get("online") is True:
                self.login_state = "online"

    def see_self_id(self, self_id: str) -> None:
        if self_id and not self.self_id:
            self.self_id = self_id

    # ---- 事件计数 ----

    def count_group_event(self, group_id: str) -> None:
        now = _utcnow_iso()
        self.last_event_at = now
        if group_id:
            if (
                len(self.group_last_event) >= _MAX_TRACKED_GROUPS
                and group_id not in self.group_last_event
            ):
                # 淘汰最早写入的一条（dict 保序）
                self.group_last_event.pop(next(iter(self.group_last_event)))
            self.group_last_event[group_id] = now

    def count_processed(self) -> None:
        self.processed_total += 1

    def count_skipped(self) -> None:
        self.skipped_total += 1

    def count_failed(self) -> None:
        self.failed_total += 1

    def count_invalid(self) -> None:
        self.invalid_total += 1

    def count_ignored(self) -> None:
        self.ignored_total += 1

    # ---- 就绪状态 ----

    def backlog(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    def state(self) -> str:
        """``ready`` / ``degraded``：任一必要条件不满足即降级。"""
        if not self.enabled:
            return "disabled"
        if not self.connected or self.login_state != "online":
            return "degraded"
        if self._last_heartbeat_mono is not None:
            if time.monotonic() - self._last_heartbeat_mono > self._heartbeat_timeout:
                return "degraded"
        elif self.connect_count > 0 and self.connected:
            # 已连接但从未收到心跳：等待首个心跳的宽限期内不算就绪
            return "degraded"
        if self.backlog() >= self._queue_max:
            return "degraded"
        return "ready"

    def snapshot(self, *, include_sensitive: bool = True) -> dict[str, object]:
        heartbeat_age: float | None = None
        if self._last_heartbeat_mono is not None:
            heartbeat_age = round(time.monotonic() - self._last_heartbeat_mono, 1)
        snapshot: dict[str, object] = {
            "state": self.state(),
            "connected": self.connected,
            "login_state": self.login_state,
            "connect_count": self.connect_count,
            "disconnect_count": self.disconnect_count,
            "last_connect_at": self.last_connect_at,
            "last_disconnect_at": self.last_disconnect_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "heartbeat_age_seconds": heartbeat_age,
            "heartbeat_timeout_seconds": self._heartbeat_timeout,
            "last_event_at": self.last_event_at,
            "queue_backlog": self.backlog(),
            "queue_max": self._queue_max,
            "processed_total": self.processed_total,
            "skipped_total": self.skipped_total,
            "failed_total": self.failed_total,
            "invalid_total": self.invalid_total,
            "ignored_total": self.ignored_total,
        }
        if include_sensitive:
            snapshot.update(
                {
                    "self_id": self.self_id,
                    "group_last_event": dict(self.group_last_event),
                    "last_error": self.last_error,
                }
            )
        return snapshot


# 进程级单例：/healthz 与 /onebot/status 共享同一事实来源
onebot_status = OneBotRuntimeStatus()


def _bearer_token(websocket: WebSocket) -> str:
    auth = websocket.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _request_bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


# ---- 队列与影子处理worker ----

_queue: asyncio.Queue[dict[str, Any]] | None = None
_worker_task: asyncio.Task[None] | None = None
_worker_loop: asyncio.AbstractEventLoop | None = None


def _ensure_worker() -> asyncio.Queue[dict[str, Any]]:
    """惰性启动影子处理worker；事件循环变化（测试/重启）时重建。

    重建前取消旧worker task，避免httpx连接/session泄漏。
    """
    global _queue, _worker_task, _worker_loop
    loop = asyncio.get_running_loop()
    if (
        _queue is not None
        and _worker_task is not None
        and _worker_loop is loop
        and not _worker_task.done()
    ):
        return _queue
    # ⑦ 重建前取消旧task（旧循环上的worker会泄漏httpx连接）
    if _worker_task is not None and not _worker_task.done():
        _worker_task.cancel()
    settings = get_settings()
    _queue = asyncio.Queue(maxsize=settings.onebot_queue_max)
    onebot_status.bind_queue(_queue)
    onebot_status.configure(
        queue_max=settings.onebot_queue_max,
        heartbeat_timeout=float(settings.onebot_heartbeat_timeout_seconds),
    )
    _worker_loop = loop
    _worker_task = loop.create_task(_worker_main(_queue))
    return _queue


_WORKER_CONCURRENCY = 3  # ⑥ 并发处理：AI 30-40s/调用时允许3条消息同时在途


async def _worker_main(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """影子处理循环：3个并发处理者共享队列，各自独立引擎实例（防频率状态竞争）。"""
    dl_client = httpx.AsyncClient(timeout=15, follow_redirects=True)

    async def _process_one() -> None:
        text_engine = TextRuleEngine()
        image_engine = ImageModerationEngine()
        while True:
            payload = await queue.get()
            try:
                async with SessionLocal() as session:
                    record = await onebot_wiring.process_onebot_event(
                        payload,
                        session,
                        text_engine=text_engine,
                        image_engine=image_engine,
                        dl_client=dl_client,
                    )
                if record is None:
                    onebot_status.count_skipped()
                else:
                    onebot_status.count_processed()
                    logger.info(
                        "[onebot-shadow] %s verdict=%s conf=%s %s",
                        record.kind,
                        record.verdict,
                        record.confidence,
                        record.reason[:60],
                    )
            except Exception as exc:  # noqa: BLE001 - 单事件失败不终止worker
                logger.exception("OneBot 事件影子处理失败")
                onebot_status.count_failed()
                onebot_status.last_error = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                queue.task_done()

    try:
        await asyncio.gather(*[_process_one() for _ in range(_WORKER_CONCURRENCY)])
    finally:
        await dl_client.aclose()


def build_onebot_router(ws_path: str) -> APIRouter:
    """构建 OneBot 反向WS路由（路径来自配置；含状态查询端点）。

    应用启动即注册就绪状态：连接建立前 state=degraded（未连接），
    不等首个事件才启用，避免"启动初期误报就绪"。
    """
    settings = get_settings()
    onebot_status.configure(
        queue_max=settings.onebot_queue_max,
        heartbeat_timeout=float(settings.onebot_heartbeat_timeout_seconds),
    )
    router = APIRouter()

    @router.get("/onebot/status")
    async def onebot_status_endpoint(request: Request) -> dict[str, object]:
        """NapCat 就绪状态证据（连接/登录/心跳/积压/每群最后事件）。"""
        supplied = _request_bearer_token(request)
        expected = settings.onebot_access_token
        if not supplied or not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail="OneBot status authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return onebot_status.snapshot()

    @router.websocket(ws_path)
    async def onebot_ws(websocket: WebSocket) -> None:
        settings = get_settings()
        supplied = _bearer_token(websocket)
        expected = settings.onebot_access_token
        if (
            not settings.onebot_ws_enabled
            or not supplied
            or not expected
            or not secrets.compare_digest(supplied, expected)
        ):
            await websocket.close(code=1008)
            return

        await websocket.accept()
        onebot_status.register_connect()
        invalid_streak = 0
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    invalid_streak += 1
                    onebot_status.count_invalid()
                    if invalid_streak >= _MAX_INVALID_STREAK:
                        await websocket.close(code=1002)
                        break
                    continue
                invalid_streak = 0
                if not isinstance(event, dict):
                    onebot_status.count_invalid()
                    continue

                self_id = event.get("self_id")
                if self_id:
                    onebot_status.see_self_id(str(self_id))

                post = event.get("post_type")
                if post == "meta_event":
                    meta_type = event.get("meta_event_type")
                    if meta_type == "lifecycle" and event.get("sub_type") == "connect":
                        onebot_status.mark_online(str(event.get("self_id") or ""))
                    elif meta_type == "heartbeat":
                        onebot_status.mark_heartbeat(event.get("status"))
                    continue
                if post == "message" and event.get("message_type") == "group":
                    # 结构校验：账号、消息、群、成员四要素齐全才能去重与判定
                    if not all(
                        str(event.get(k) or "")
                        for k in ("self_id", "message_id", "group_id", "user_id")
                    ):
                        onebot_status.count_invalid()
                        continue
                    onebot_status.count_group_event(str(event.get("group_id") or ""))
                    queue = _ensure_worker()
                    # 队列满时阻塞接收（对NapCat形成TCP背压），不丢事件
                    await queue.put(event)
                    continue
                # notice（含群撤回通知，T-205待标注事件）/请求/私聊等：仅计数
                onebot_status.count_ignored()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - 连接异常按断线处理
            logger.exception("OneBot WebSocket 连接异常")
            onebot_status.register_disconnect(f"{type(exc).__name__}: {exc}")
        else:
            onebot_status.register_disconnect()
        finally:
            if onebot_status.connected:
                onebot_status.register_disconnect()

    return router
