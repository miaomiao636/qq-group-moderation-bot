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
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

import httpx
from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.db import SessionLocal
from app.moderation.image_engine import ImageModerationEngine
from app.moderation.rules import FrequencyTracker, TextRuleEngine
from app.runtime import inbox, onebot_wiring
from app.runtime.onebot_actions import onebot_action_hub

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
        self._queue: asyncio.Queue[str] | None = None
        self.storage_available = True
        self.durable_backlog = 0
        self._queue_max = 0
        self._heartbeat_timeout = 90.0

    def configure(self, *, queue_max: int, heartbeat_timeout: float) -> None:
        self._queue_max = queue_max
        self._heartbeat_timeout = heartbeat_timeout
        self.enabled = True

    def bind_queue(self, queue: asyncio.Queue[str]) -> None:
        self._queue = queue

    # ---- 连接生命周期 ----

    def register_connect(self) -> None:
        self.connected = True
        self.connect_count += 1
        self.last_connect_at = _utcnow_iso()
        self.login_state = "unknown"
        self._last_heartbeat_mono = None

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
        return max(self.durable_backlog, self._queue.qsize() if self._queue is not None else 0)

    def state(self) -> str:
        """``ready`` / ``degraded``：任一必要条件不满足即降级。"""
        if not self.enabled:
            return "disabled"
        if not self.storage_available or not self.connected or self.login_state != "online":
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
            "storage_available": self.storage_available,
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

_queue: asyncio.Queue[str] | None = None
_worker_task: asyncio.Task[None] | None = None
_worker_loop: asyncio.AbstractEventLoop | None = None
_worker_wake: asyncio.Event | None = None
_worker_stop: asyncio.Event | None = None
_runtime_lock: BinaryIO | None = None


def moderation_worker_healthy() -> bool:
    """A connected QQ session alone does not prove its consumer is alive."""
    return _worker_task is not None and not _worker_task.done()


def _ensure_worker() -> asyncio.Queue[str]:
    """惰性启动影子处理worker；事件循环变化（测试/重启）时重建。

    重建前取消旧worker task，避免httpx连接/session泄漏。
    """
    global _queue, _worker_task, _worker_loop, _worker_wake, _worker_stop
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
    _worker_wake = asyncio.Event()
    _worker_stop = asyncio.Event()
    onebot_status.bind_queue(_queue)
    onebot_status.configure(
        queue_max=settings.onebot_queue_max,
        heartbeat_timeout=float(settings.onebot_heartbeat_timeout_seconds),
    )
    _worker_loop = loop
    _worker_task = loop.create_task(_worker_main(_queue))
    return _queue


# 并发处理（2026-09-15 容量整改：3→10，负责人授权）：AI 等待为 IO-bound，
# 单 worker 处理期大部分时间在等 AI 回复；10 路在途支撑 ≈350 条/分钟（压测复验）。
_WORKER_CONCURRENCY = 10


async def _worker_main(queue: asyncio.Queue[str]) -> None:
    """Dispatch committed inbox keys; the DB is authoritative across process restarts."""
    from sqlalchemy import func, select

    from app.models import ProcessedEvent

    dl_client = httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
    scheduled: set[str] = set()
    # This runtime accepts OneBot only. Share its synchronous same-group/member
    # counter, but keep rule snapshots isolated per worker to avoid cross-group
    # mutation if the processing pipeline awaits between stages.
    frequency_tracker = FrequencyTracker()
    wake = _worker_wake
    stopping = _worker_stop
    assert wake is not None
    assert stopping is not None

    async def _dispatch() -> None:
        last_cleanup = 0.0
        while not stopping.is_set():
            wake.clear()
            try:
                async with SessionLocal() as session:
                    settings = get_settings()
                    if time.monotonic() - last_cleanup >= 60:
                        await inbox.purge_inbox(
                            session,
                            raw_retention_days=settings.raw_retention_days,
                            decision_retention_days=settings.decision_retention_days,
                        )
                        last_cleanup = time.monotonic()
                    keys = await inbox.due_keys(session, limit=settings.onebot_queue_max)
                    onebot_status.durable_backlog = int(
                        (
                            await session.execute(
                                select(func.count())
                                .select_from(inbox.InboxEvent)
                                .where(inbox.InboxEvent.status.in_(("PENDING", "PROCESSING")))
                            )
                        ).scalar_one()
                    )
                onebot_status.storage_available = True
                for key in keys:
                    if key not in scheduled and not queue.full():
                        scheduled.add(key)
                        queue.put_nowait(key)
            except Exception as exc:  # noqa: BLE001 - DB errors must degrade, with bounded polling
                onebot_status.storage_available = False
                onebot_status.last_error = f"durable_inbox:{type(exc).__name__}"
                logger.warning("OneBot durable inbox unavailable (%s)", type(exc).__name__)
            if not stopping.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=1.0)

    async def _renew_while_running(claim: inbox.InboxClaim, task: asyncio.Task[Any]) -> Any:
        try:
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=60)
                except TimeoutError:
                    async with SessionLocal() as session:
                        if not await inbox.renew_event(session, claim):
                            raise RuntimeError("inbox lease lost") from None
        finally:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _process_one() -> None:
        text_engine = TextRuleEngine(frequency_tracker=frequency_tracker)
        image_engine = ImageModerationEngine()
        while not stopping.is_set():
            try:
                async with asyncio.timeout(1.0):
                    key = await queue.get()
            except TimeoutError:
                continue
            claim = None
            try:
                async with SessionLocal() as session:
                    claim = await inbox.claim_event(session, key)
                if claim is None:
                    continue

                async def _process(current_claim: inbox.InboxClaim = claim) -> Any:
                    async with SessionLocal() as session:
                        return await onebot_wiring.process_onebot_event(
                            current_claim.payload,
                            session,
                            text_engine=text_engine,
                            image_engine=image_engine,
                            dl_client=dl_client,
                        )

                record = await _renew_while_running(claim, asyncio.create_task(_process()))
                async with SessionLocal() as session:
                    processed = await session.get(ProcessedEvent, key)
                    if record is not None or (
                        processed is not None and processed.status == "PROCESSED"
                    ):
                        await inbox.finish_event(session, claim)
                        onebot_status.count_processed()
                    elif processed is not None and processed.status == "DEAD":
                        await inbox.finish_event(session, claim, dead=True)
                        onebot_status.count_skipped()
                    elif processed is not None and processed.status == "PROCESSING":
                        await inbox.defer_event(
                            session,
                            claim,
                            until=processed.lease_expires_at
                            or datetime.now(UTC) + timedelta(seconds=30),
                        )
                    else:
                        await inbox.retry_event(session, claim, "processing_failed")
                        onebot_status.count_failed()
            except Exception as exc:  # noqa: BLE001 - 单事件失败不终止worker
                logger.warning("OneBot inbox processing failed (%s)", type(exc).__name__)
                onebot_status.count_failed()
                onebot_status.last_error = f"inbox_processing:{type(exc).__name__}"
                if claim is not None:
                    try:
                        async with SessionLocal() as session:
                            await inbox.retry_event(session, claim, type(exc).__name__)
                    except Exception:  # noqa: BLE001 - lease remains recoverable after DB returns
                        onebot_status.storage_available = False
            finally:
                scheduled.discard(key)
                queue.task_done()
                wake.set()

    tasks = [asyncio.create_task(_dispatch())] + [
        asyncio.create_task(_process_one()) for _ in range(_WORKER_CONCURRENCY)
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dl_client.aclose()


async def start_onebot_runtime() -> None:
    global _runtime_lock
    if _runtime_lock is not None:
        raise RuntimeError("OneBot runtime is already owned by another process")
    settings = get_settings()
    _runtime_lock = inbox.acquire_runtime_lock(settings.database_url)
    try:
        async with SessionLocal() as session:
            recovered = await inbox.recover_abandoned_actions(session)
        if recovered:
            logger.warning("OneBot recovered %d abandoned actions as UNKNOWN", recovered)
    except BaseException:
        _runtime_lock.close()
        _runtime_lock = None
        raise
    onebot_action_hub.configure(
        timeout_seconds=float(settings.onebot_action_timeout_seconds),
        readiness=onebot_status.state,
        expected_self_id=settings.onebot_self_id,
    )
    _ensure_worker()


async def stop_onebot_runtime() -> None:
    global _queue, _worker_task, _worker_loop, _worker_wake, _worker_stop, _runtime_lock
    if _worker_stop is not None:
        _worker_stop.set()
    if _worker_wake is not None:
        _worker_wake.set()
    if _worker_task is not None:
        # Let SQLite transactions/session cleanup finish before cancellation. This
        # also avoids double cancellation while the driver's connection opens.
        _, pending = await asyncio.wait([_worker_task], timeout=2.0)
        if pending:
            _worker_task.cancel()
        await asyncio.gather(_worker_task, return_exceptions=True)
    _queue = None
    _worker_task = None
    _worker_loop = None
    _worker_wake = None
    _worker_stop = None
    if _runtime_lock is not None:
        _runtime_lock.close()
        _runtime_lock = None
    onebot_action_hub.reset()
    onebot_status.register_disconnect()


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
    # T-307：出站动作通道复用本连接；就绪语义与 /onebot/status 一致
    onebot_action_hub.configure(
        timeout_seconds=float(settings.onebot_action_timeout_seconds),
        readiness=onebot_status.state,
        expected_self_id=settings.onebot_self_id,
    )
    router = APIRouter()

    @router.get("/onebot/status")
    async def onebot_status_endpoint(request: Request) -> dict[str, object]:
        """NapCat 就绪状态证据（连接/登录/心跳/积压/每群最后事件）。"""
        supplied = _request_bearer_token(request)
        expected = settings.onebot_access_token
        if (
            not supplied
            or not expected
            or not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
        ):
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
            or not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
        ):
            await websocket.close(code=1008)
            return

        header_self_id = websocket.headers.get("x-self-id") or ""
        if not onebot_action_hub.bind(websocket, self_id=header_self_id):
            await websocket.close(code=1008)
            return
        invalid_streak = 0
        try:
            await websocket.accept()
            onebot_status.register_connect()
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode("utf-8")) > inbox.MAX_PAYLOAD_BYTES:
                    onebot_status.count_invalid()
                    await websocket.close(code=1009)
                    break
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

                # T-307：NapCat 对动作调用的 echo 响应优先匹配出站通道
                if onebot_action_hub.handle_response(event, websocket=websocket):
                    continue

                self_id = event.get("self_id")
                if not self_id:
                    onebot_status.count_invalid()
                    continue
                if not onebot_action_hub.bind(websocket, self_id=str(self_id)):
                    onebot_status.count_invalid()
                    await websocket.close(code=1008)
                    break
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
                    try:
                        inbox.validate_event(event)
                    except ValueError:
                        onebot_status.count_invalid()
                        continue
                    onebot_status.count_group_event(str(event.get("group_id") or ""))
                    _ensure_worker()
                    try:
                        # A disconnect must not interrupt a half-finished durable
                        # admission or its SQLite connection cleanup.
                        with CancelScope(shield=True):
                            async with SessionLocal() as session:
                                await inbox.enqueue_event(
                                    session, event, max_pending=settings.onebot_queue_max
                                )
                    except (inbox.InboxFull, ValueError) as exc:
                        onebot_status.count_failed()
                        onebot_status.last_error = f"inbox_admission:{type(exc).__name__}"
                        await websocket.close(code=1013)
                        break
                    except Exception as exc:  # noqa: BLE001 - uncommitted events are never scheduled
                        onebot_status.storage_available = False
                        onebot_status.count_failed()
                        onebot_status.last_error = f"inbox_storage:{type(exc).__name__}"
                        await websocket.close(code=1011)
                        break
                    if _worker_wake is not None:
                        _worker_wake.set()
                    continue
                if post == "notice" and event.get("notice_type") == "group_recall":
                    from app.actions.recall_confirmation import accept_notice
                    from app.adapters.onebot.recall_notice import parse_recall_notice

                    notice = parse_recall_notice(event, expected_self_id=settings.onebot_self_id)
                    if notice is None:
                        onebot_status.count_invalid()
                        continue
                    try:
                        with CancelScope(shield=True):
                            async with SessionLocal() as session:
                                await accept_notice(session, notice)
                    except Exception as exc:  # noqa: BLE001 - no false confirmation on storage loss
                        onebot_status.storage_available = False
                        onebot_status.count_failed()
                        onebot_status.last_error = f"recall_notice_storage:{type(exc).__name__}"
                        await websocket.close(code=1011)
                        break
                    continue
                # Other notices / requests / private messages do not become labels.
                onebot_status.count_ignored()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - 连接异常按断线处理
            logger.exception("OneBot WebSocket 连接异常")
            onebot_status.register_disconnect(f"{type(exc).__name__}: {exc}")
        else:
            onebot_status.register_disconnect()
        finally:
            onebot_action_hub.unbind(websocket)
            if onebot_status.connected:
                onebot_status.register_disconnect()

    return router
