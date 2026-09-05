"""AccessToken 获取与缓存（T-102）。

实测结论（决策 D-012）：
- 令牌签发：`POST {api_base}/app/getAppAccessToken`，body 为 appId/clientSecret；
- 业务 API 基址 `https://api.bot.qq.com`，请求头 `Authorization: QQBot <token>`；
- 旧组合 `api.sgroup.qq.com` + `QQey` 返回 401，禁止使用。
令牌有效期约 7200 秒，过期前提前刷新，避免临界过期请求失败。
"""

from __future__ import annotations

import time

import httpx
from pydantic import BaseModel

_REFRESH_MARGIN_SECONDS = 120


class TokenError(RuntimeError):
    """令牌获取失败。"""


class CachedToken(BaseModel):
    access_token: str
    expires_at: float


class TokenManager:
    """缓存 AccessToken；线程安全依赖调用方串行化（适配器内单任务循环）。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        api_base: str = "https://api.bot.qq.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not app_id or not app_secret:
            raise TokenError("QQ_APP_ID / QQ_APP_SECRET 未配置，无法获取访问令牌")
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_url = f"{api_base.rstrip('/')}/app/getAppAccessToken"
        self._client = client or httpx.AsyncClient(timeout=10)
        self._owns_client = client is None
        self._cached: CachedToken | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_token(self) -> str:
        """返回有效令牌；缓存剩余有效期不足时刷新。"""
        if self._cached and self._cached.expires_at - time.monotonic() > _REFRESH_MARGIN_SECONDS:
            return self._cached.access_token
        try:
            resp = await self._client.post(
                self._token_url,
                json={"appId": self._app_id, "clientSecret": self._app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TokenError(f"获取 AccessToken 失败: {exc}") from exc
        token = str(data.get("access_token") or "")
        if not token:
            raise TokenError(f"令牌响应缺少 access_token: {data}")
        expires_in = int(data.get("expires_in") or 7200)
        self._cached = CachedToken(access_token=token, expires_at=time.monotonic() + expires_in)
        return token

    def auth_header_value(self, token: str) -> str:
        return f"QQBot {token}"
