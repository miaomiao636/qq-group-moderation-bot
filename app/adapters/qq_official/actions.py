"""官方动作适配器：撤回 / 禁言 / 警告（T-102）。

架构约束（D-001）：本适配器**不含任何踢人逻辑**。
实测结论（D-012/D-013）：
- 撤回：DELETE /v2/groups/{group_openid}/messages/{message_id}，接口幂等（重复撤回返回200）；
- 禁言：POST /v2/groups/{group_openid}/restrict_chat_setting，`mute_expire_at` 为
  RFC3339 到期时间（Asia/Shanghai），单批≤20人，最长30天；群主/管理员/机器人被平台拒绝（40103004）；
- 警告：以被动回复实现（POST /v2/groups/{group_openid}/messages，携带被审消息 id 作为
  msg_id 上下文）。被动回复**不做自动重试**，避免重复警告刷屏。

所有动作：超时受限、有限重试（可幂等者）、结果可审计（ActionResult + ActionLog 持久化由调用方完成）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from app.adapters.qq_official.auth import TokenManager

CITY_TZ = ZoneInfo("Asia/Shanghai")

_MAX_MUTE_SECONDS = 30 * 24 * 3600
_ACTION_TIMEOUT = httpx.Timeout(10.0)
_MAX_ATTEMPTS_IDEMPOTENT = 2  # 撤回/禁言可安全重试
_MAX_ATTEMPTS_NON_IDEMPOTENT = 1  # 警告不可重试

ActionName = Literal["recall", "mute", "unmute", "warn"]


class ActionNotConfiguredError(RuntimeError):
    """动作适配器未正确配置（缺少令牌等）。"""


class ActionResult(BaseModel):
    """动作执行结果，可审计。"""

    action: ActionName
    ok: bool
    status_code: int | None = None
    err_code: int | None = None
    err_message: str = ""
    attempts: int = 0

    @property
    def is_permission_error(self) -> bool:
        """权限不足类错误（实测：撤回40062003无操作权限；禁言40103004保护角色）。"""
        return self.err_code in (40062003, 40103004)


def _mute_expire_at(seconds: int) -> str:
    """按 Asia/Shanghai 计算 RFC3339 到期时间。"""
    expire = datetime.now(CITY_TZ) + timedelta(seconds=seconds)
    return expire.isoformat()


def _err_from_body(body: dict[str, object]) -> tuple[int | None, str]:
    code = body.get("err_code") or body.get("code")
    message = str(body.get("message") or "")
    return (
        int(code) if isinstance(code, (int, str)) and str(code).lstrip("-").isdigit() else None
    ), message


class OfficialActionAdapter:
    """官方动作适配器。构造需 TokenManager；所有方法返回 ActionResult，不抛业务异常。"""

    def __init__(
        self,
        token_manager: TokenManager,
        api_base: str = "https://api.bot.qq.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token_manager = token_manager
        self._base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=_ACTION_TIMEOUT, follow_redirects=True)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _group_url(self, group_openid: str, path: str) -> str:
        return f"{self._base}/v2/groups/{group_openid}/{path}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, object] | None = None,
        max_attempts: int = _MAX_ATTEMPTS_IDEMPOTENT,
    ) -> ActionResult:
        """带超时与有限重试的请求；网络错误重试，HTTP 错误响应不重试（返回错误体）。"""
        last: ActionResult | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                token = await self._token_manager.get_token()
                resp = await self._client.request(
                    method,
                    url,
                    headers={"Authorization": self._token_manager.auth_header_value(token)},
                    json=json_body,
                )
                try:
                    body: dict[str, object] = resp.json()
                except ValueError:
                    body = {}
                err_code, err_message = _err_from_body(body)
                # 2xx 视为成功；QQ 部分异步接口返回 202/204 也算受理成功
                ok = 200 <= resp.status_code < 300
                result = ActionResult(
                    action="recall",  # 由调用方修正具体动作名
                    ok=ok,
                    status_code=resp.status_code,
                    err_code=None if ok else err_code,
                    err_message="" if ok else err_message,
                    attempts=attempt,
                )
                if ok:
                    return result
                last = result
                # 业务错误（4xx/5xx）不重试：重复请求副作用或无意义
                return result
            except httpx.HTTPError as exc:
                last = ActionResult(
                    action="recall",
                    ok=False,
                    err_message=f"网络错误: {exc}",
                    attempts=attempt,
                )
        assert last is not None
        return last

    async def recall(
        self, group_openid: str, message_id: str, *, actor: str = "system"
    ) -> ActionResult:
        """撤回群消息（幂等，可重试）。"""
        result = await self._request(
            "DELETE",
            self._group_url(group_openid, f"messages/{message_id}"),
            max_attempts=_MAX_ATTEMPTS_IDEMPOTENT,
        )
        return result.model_copy(update={"action": "recall"})

    async def mute(
        self,
        group_openid: str,
        member_openid: str,
        seconds: int,
        *,
        actor: str = "system",
    ) -> ActionResult:
        """禁言普通成员。seconds 范围 1..30天；到期时间按 Asia/Shanghai 计算。"""
        if seconds < 1 or seconds > _MAX_MUTE_SECONDS:
            return ActionResult(
                action="mute",
                ok=False,
                err_message=f"禁言时长 {seconds}s 超出允许范围 1..{_MAX_MUTE_SECONDS}",
                attempts=0,
            )
        body: dict[str, object] = {
            "members": [
                {
                    "op": "add",
                    "member_openid": member_openid,
                    "mute_expire_at": _mute_expire_at(seconds),
                }
            ]
        }
        result = await self._request(
            "POST",
            self._group_url(group_openid, "restrict_chat_setting"),
            json_body=body,
            max_attempts=_MAX_ATTEMPTS_IDEMPOTENT,
        )
        return result.model_copy(update={"action": "mute"})

    async def unmute(
        self, group_openid: str, member_openid: str, *, actor: str = "system"
    ) -> ActionResult:
        """解除禁言：op=del + 空 mute_expire_at（实测 D-012）。"""
        body: dict[str, object] = {
            "members": [
                {
                    "op": "del",
                    "member_openid": member_openid,
                    "mute_expire_at": "",
                }
            ]
        }
        result = await self._request(
            "POST",
            self._group_url(group_openid, "restrict_chat_setting"),
            json_body=body,
            max_attempts=_MAX_ATTEMPTS_IDEMPOTENT,
        )
        return result.model_copy(update={"action": "unmute"})

    async def warn(
        self,
        group_openid: str,
        reply_to_message_id: str,
        text: str,
        *,
        msg_seq: int = 1,
        actor: str = "system",
    ) -> ActionResult:
        """以被动回复发送警告文本。

        被动回复依赖被回复消息仍在时限内（官方约5分钟），且**不重试**（避免重复警告刷屏）。
        """
        if not text.strip():
            return ActionResult(action="warn", ok=False, err_message="警告内容为空", attempts=0)
        body: dict[str, object] = {
            "content": text,
            "msg_type": 0,
            "msg_id": reply_to_message_id,
            "msg_seq": msg_seq,
        }
        result = await self._request(
            "POST",
            self._group_url(group_openid, "messages"),
            json_body=body,
            max_attempts=_MAX_ATTEMPTS_NON_IDEMPOTENT,
        )
        return result.model_copy(update={"action": "warn"})
