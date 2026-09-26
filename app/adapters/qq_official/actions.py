"""官方自动审核动作适配器：仅撤回（T-102）。

架构约束（D-001）：本适配器**不含任何踢人逻辑**。
实测结论（D-012/D-013）：
- 撤回：DELETE /v2/groups/{group_openid}/messages/{message_id}，接口幂等（重复撤回返回200）；
- 旧禁言的人工解除入口保留为 ``unmute``；不再创建禁言或群内违规警告。

所有动作：超时受限、有限重试（可幂等者）、结果可审计（ActionResult + ActionLog 持久化由调用方完成）。
"""

from __future__ import annotations

import httpx

from app.adapters.qq_official.auth import TokenManager
from app.core.contracts import ActionResult  # T-305：结果契约上移至中立核心

# 兼容再导出：既有调用方 `from ...actions import ActionResult` 不受影响。
__all__ = [
    "ActionNotConfiguredError",
    "ActionResult",
    "OfficialActionAdapter",
]

_ACTION_TIMEOUT = httpx.Timeout(10.0)
_MAX_ATTEMPTS_IDEMPOTENT = 2  # 撤回和人工解除旧禁言可安全重试


class ActionNotConfiguredError(RuntimeError):
    """动作适配器未正确配置（缺少令牌等）。"""


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
        *,
        owns_token_manager: bool = False,
    ) -> None:
        self._token_manager = token_manager
        self._owns_token_manager = owns_token_manager
        self._base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=_ACTION_TIMEOUT, follow_redirects=True)
        self._owns_client = client is None

    async def aclose(self) -> None:
        try:
            if self._owns_client:
                await self._client.aclose()
        finally:
            if self._owns_token_manager:
                await self._token_manager.aclose()

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
