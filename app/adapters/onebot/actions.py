"""NapCat/OneBot 11 管理动作适配器（T-307）。

架构约束：
- 实现 ``app.core.contracts.ModerationActionClient`` 契约（recall/mute/warn），
  **不含任何踢人逻辑**（踢人必须人工审批，T-304）；
- 不在 Adapter 内重复审核逻辑——意图、幂等键、保护角色、急停、按群开关
  均由 provider-neutral orchestrator（``app.actions.orchestrator``）先行裁决；
- 出站调用经注入的 ``caller``（组合根在 runtime 层提供反向WS echo 通道），
  本模块不持有任何连接或 I/O 状态，可独立单测；
- OneBot 数字 ID 强制校验：``group_id/user_id/message_id`` 必须可解析为
  int64；非数字一律 FAILED（确定未执行），绝不伪装或猜测。

结果语义（与 orchestrator 的 UNKNOWN 冻结策略配合）：
- 发送前失败（未就绪/未绑定连接）→ FAILED（确定没有发出请求）；
- 发送后超时/连接丢失 → 抛 ``OneBotActionError``（结果不确定）→
  orchestrator 冻结为 UNKNOWN，禁止自动重放；
- 响应 status=ok/async → SUCCEEDED；status=failed → FAILED（明确拒绝，
  含 NapCat 权限不足等场景，可审计）。

警告（send_group_msg）非幂等，不做自动重试；撤回/禁言同样只单次调用，
UNKNOWN 意图的重放必须由人工触发。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from app.core.contracts import ActionResult

__all__ = [
    "OneBotActionCaller",
    "OneBotActionClient",
    "OneBotActionError",
]

# 出站调用 seam：action 名称 + params → NapCat 的响应 dict。
# 由组合根（runtime 层反向WS hub）注入；抛 OneBotActionError 表示传输层失败。
OneBotActionCaller = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]

_MAX_MUTE_SECONDS = 30 * 24 * 3600


class OneBotActionError(RuntimeError):
    """OneBot 动作传输失败。

    kind 语义：
    - ``not_ready`` / ``disconnected``：发送前失败，确定未执行；
    - ``timeout`` / ``response_lost``：发送后结果不确定（UNKNOWN，禁重放）。
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _numeric_id(value: str, field: str) -> int:
    """把外部 ID 解析为 OneBot int64；非数字直接抛 ValueError（确定失败）。"""
    text = str(value).strip()
    if not text or not text.lstrip("-").isdigit():
        raise ValueError(f"{field}={value!r} 非数字，OneBot动作拒绝执行")
    return int(text)


class OneBotActionClient:
    """OneBot 11 动作客户端：recall=delete_msg，mute=set_group_ban，warn=send_group_msg。"""

    def __init__(self, caller: OneBotActionCaller) -> None:
        self._caller = caller

    async def _call(
        self, action_name: str, endpoint: str, params: Mapping[str, Any]
    ) -> ActionResult:
        try:
            resp = await self._caller(endpoint, params)
        except OneBotActionError as exc:
            if exc.kind in ("not_ready", "disconnected"):
                # 发送前失败：确定没有请求发出，明确 FAILED 而非 UNKNOWN。
                return ActionResult(
                    action=action_name,
                    ok=False,
                    err_message=f"NapCat未就绪或未连接（{exc}），未发出请求",
                    attempts=1,
                )
            raise  # timeout / response_lost：结果不确定，交由 orchestrator 冻结 UNKNOWN

        if not isinstance(resp, Mapping) or "status" not in resp:
            raise OneBotActionError(
                "response_lost", f"OneBot响应结构非法：{type(resp).__name__} 缺少 status"
            )
        status = str(resp.get("status") or "")
        retcode = resp.get("retcode")
        err_code = int(retcode) if isinstance(retcode, (int, float)) else None
        wording = str(resp.get("wording") or resp.get("message") or "")
        if status in ("ok", "async"):
            return ActionResult(action=action_name, ok=True, status_code=0, attempts=1)
        return ActionResult(
            action=action_name,
            ok=False,
            status_code=0,
            err_code=err_code,
            err_message=wording or f"OneBot动作失败 status={status}",
            attempts=1,
        )

    async def recall(
        self, external_group_id: str, external_message_id: str, /, *, actor: str = "system"
    ) -> ActionResult:
        try:
            message_id = _numeric_id(external_message_id, "message_id")
        except ValueError as exc:
            return ActionResult(action="recall", ok=False, err_message=str(exc), attempts=1)
        return await self._call("recall", "delete_msg", {"message_id": message_id})

    async def mute(
        self,
        external_group_id: str,
        external_user_id: str,
        seconds: int,
        /,
        *,
        actor: str = "system",
    ) -> ActionResult:
        try:
            group_id = _numeric_id(external_group_id, "group_id")
            user_id = _numeric_id(external_user_id, "user_id")
        except ValueError as exc:
            return ActionResult(action="mute", ok=False, err_message=str(exc), attempts=1)
        if seconds <= 0 or seconds > _MAX_MUTE_SECONDS:
            return ActionResult(
                action="mute",
                ok=False,
                err_message=f"禁言时长非法：{seconds}秒（允许 1~{_MAX_MUTE_SECONDS}）",
                attempts=1,
            )
        return await self._call(
            "mute", "set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": seconds}
        )

    async def warn(
        self,
        external_group_id: str,
        reply_to_message_id: str,
        text: str,
        /,
        *,
        msg_seq: int = 1,
        actor: str = "system",
    ) -> ActionResult:
        try:
            group_id = _numeric_id(external_group_id, "group_id")
            reply_id = _numeric_id(reply_to_message_id, "reply_to_message_id")
        except ValueError as exc:
            return ActionResult(action="warn", ok=False, err_message=str(exc), attempts=1)
        if not text.strip():
            return ActionResult(action="warn", ok=False, err_message="警告文本为空", attempts=1)
        message: list[dict[str, Any]] = [
            {"type": "reply", "data": {"id": reply_id}},
            {"type": "text", "data": {"text": text}},
        ]
        return await self._call(
            "warn", "send_group_msg", {"group_id": group_id, "message": message}
        )
