"""管理后台登录会话（T-301）。

- 凭据来自 settings（ADMIN_USERNAME/ADMIN_PASSWORD，prod 强制非空密码）；
- 会话令牌随机生成、内存保存（进程重启即失效，安全侧合理）；
- Cookie 仅 HttpOnly；本机部署不强制 HTTPS（127.0.0.1 明文可接受，公网部署前必须加反代TLS）。
"""

from __future__ import annotations

import secrets

from app.config import get_settings

_SESSIONS: set[str] = set()
_CSRF_TOKENS: dict[str, str] = {}

SESSION_COOKIE = "admin_session"


class AuthError(ValueError):
    """登录失败。"""


def login(username: str, password: str) -> str:
    """校验凭据，成功返回会话令牌。"""
    settings = get_settings()
    if not settings.admin_password.strip():
        raise AuthError("管理后台未设置 ADMIN_PASSWORD，拒绝登录（请在 .env 配置后重启）")
    if not secrets.compare_digest(username, settings.admin_username) or not secrets.compare_digest(
        password, settings.admin_password
    ):
        raise AuthError("用户名或密码错误")
    token = secrets.token_urlsafe(32)
    _SESSIONS.add(token)
    _CSRF_TOKENS[token] = secrets.token_urlsafe(24)
    return token


def logout(token: str) -> None:
    _SESSIONS.discard(token)
    _CSRF_TOKENS.pop(token, None)


def is_valid(token: str | None) -> bool:
    return bool(token) and token in _SESSIONS


def csrf_token(session_token: str) -> str:
    """Return a stable CSRF token for the current in-memory admin session."""
    token = _CSRF_TOKENS.get(session_token)
    if token is None:
        token = secrets.token_urlsafe(24)
        _CSRF_TOKENS[session_token] = token
    return token


def validate_csrf(session_token: str | None, submitted: str | None) -> bool:
    """Validate a submitted CSRF token for a logged-in admin session."""
    if not session_token or not submitted:
        return False
    expected = _CSRF_TOKENS.get(session_token)
    if expected is None:
        return False
    return secrets.compare_digest(expected, submitted)
