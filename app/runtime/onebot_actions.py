"""反向WebSocket上的 OneBot 出站动作通道（T-307）。

``OneBotActionHub`` 复用 NapCat 主动连入的反向WS连接出站调用 OneBot API：
- 发送 ``{"action": ..., "params": ..., "echo": uuid}`` 并等待 NapCat 回带
  相同 echo 的响应（OneBot 11 反向WS标准语义）；
- 就绪检查：NapCat 不在 ``ready`` 状态（未连接/心跳超时/队列积压）时
  拒绝发送——发送前失败是确定的，不会造成 UNKNOWN；
- 发送后超时/连接替换/断开：结果不确定，抛 ``OneBotActionError``
  （kind=timeout/response_lost），由 orchestrator 冻结为 UNKNOWN；
- 并发安全：echo 匹配 + 事件循环内 future；发送用锁串行化，避免
  多任务交错写坏WS帧。

本模块属 runtime 组合根，允许导入 Adapter；审核核心不允许。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from app.adapters.onebot.actions import OneBotActionError

logger = logging.getLogger(__name__)


class OneBotActionHub:
    """管理当前 NapCat 反向WS连接与待响应动作的 echo 注册表。"""

    def __init__(self) -> None:
        self._ws: Any | None = None
        self._pending: dict[str, asyncio.Future[Mapping[str, Any]]] = {}
        self._send_lock: asyncio.Lock | None = None
        self._timeout_seconds: float = 10.0
        # 就绪检查回调（由 onebot_ws 注入，返回 ready/degraded），避免循环导入
        self._readiness: Callable[[], str] = lambda: "degraded"

    def configure(self, *, timeout_seconds: float, readiness: Callable[[], str]) -> None:
        self._timeout_seconds = timeout_seconds
        self._readiness = readiness

    # ---- 连接生命周期（由 onebot_ws 的 WS 处理器调用） ----

    def bind(self, websocket: Any) -> None:
        """新连接接管出站通道；旧连接上的待响应动作按结果不确定处理。"""
        old = self._ws
        self._ws = websocket
        self._send_lock = asyncio.Lock()
        if old is not None and old is not websocket:
            self._fail_pending("response_lost", "NapCat重连，旧连接上的动作结果不确定")

    def unbind(self, websocket: Any) -> None:
        """连接断开；仅当是当前绑定连接时清空通道并失败所有待响应动作。"""
        if self._ws is not websocket:
            return
        self._ws = None
        self._fail_pending("response_lost", "NapCat连接断开，动作结果不确定")

    def _fail_pending(self, kind: str, reason: str) -> None:
        for echo, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(OneBotActionError(kind, reason))
            self._pending.pop(echo, None)

    def handle_response(self, payload: Mapping[str, Any]) -> bool:
        """尝试把入站帧匹配为动作响应；命中返回 True（调用方跳过事件处理）。"""
        echo = payload.get("echo")
        if not isinstance(echo, str) or not echo:
            return False
        # 带 post_type 的一定是事件而不是动作响应，防御性排除
        if payload.get("post_type"):
            return False
        fut = self._pending.get(echo)
        if fut is None or fut.done():
            return False
        fut.set_result(dict(payload))
        self._pending.pop(echo, None)
        return True

    # ---- 出站调用（ModerationActionClient 的 caller seam） ----

    @property
    def ready(self) -> bool:
        return self._readiness() == "ready"

    async def call(self, action: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """发送 OneBot API 调用并等待 echo 响应。

        - 未绑定连接或 NapCat 非 ready：``not_ready``/``disconnected``（未发出）；
        - 超时或等待中连接被替换：``timeout``/``response_lost``（结果不确定）。
        """
        if self._ws is None:
            raise OneBotActionError("disconnected", "NapCat反向WS未连接，未发出请求")
        if not self.ready:
            raise OneBotActionError("not_ready", "NapCat就绪状态非ready，未发出请求")
        loop = asyncio.get_running_loop()
        echo = uuid.uuid4().hex
        fut: asyncio.Future[Mapping[str, Any]] = loop.create_future()
        self._pending[echo] = fut
        frame = json.dumps({"action": action, "params": dict(params), "echo": echo})
        try:
            assert self._send_lock is not None
            async with self._send_lock:
                # 锁内复查连接仍为发起时的连接（重连后旧引用不可再用）
                if self._ws is None:
                    raise OneBotActionError("disconnected", "等待发送时NapCat连接已断开")
                await self._ws.send_text(frame)
        except OneBotActionError:
            self._pending.pop(echo, None)
            raise
        except Exception as exc:  # noqa: BLE001 - 发送失败结果不确定（可能已到达）
            self._pending.pop(echo, None)
            raise OneBotActionError(
                "response_lost", f"动作发送失败（结果不确定）：{type(exc).__name__}: {exc}"
            ) from exc
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout_seconds)
        except TimeoutError:
            self._pending.pop(echo, None)
            raise OneBotActionError(
                "timeout", f"OneBot动作 {action} 超时（{self._timeout_seconds}s），结果不确定"
            ) from None
        finally:
            self._pending.pop(echo, None)


# 进程级单例：onebot_ws 的连接生命周期与本模块共享同一事实来源
onebot_action_hub = OneBotActionHub()
