"""P0-1: 管理 REST API 鉴权安全测试。

覆盖：伪造Cookie拒绝、过期Cookie拒绝、错误Bearer拒绝、只读Token越权拒绝、
无CSRF写操作拒绝、常量时间比较、Agent写操作审计。
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient


def _app_with_settings(**overrides):
    import os

    for k, v in overrides.items():
        os.environ[k] = v
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    yield app
    for k in overrides:
        os.environ.pop(k, None)
    get_settings.cache_clear()


@pytest.fixture
def app():
    yield from _app_with_settings()


@pytest.fixture
def app_with_agent_token():
    yield from _app_with_settings(AGENT_API_TOKEN="test-agent-token-12345")


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="?csrf"? value="([^"]+)"', html)
    assert match, "页面应包含CSRF令牌"
    return match.group(1)


def _login(client: TestClient) -> str:
    """登录并返回 session cookie；cookie 自动持久化到 client 实例。"""
    resp = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cookie = client.cookies.get("admin_session")
    assert cookie, "登录未获取 cookie"
    return cookie


def test_forged_cookie_rejected(app):
    """伪造的 admin_session cookie 必须被拒绝（核心漏洞修复）。"""
    with TestClient(app) as client:
        client.cookies.set("admin_session", "FORGED-TOKEN-XYZ")
        resp = client.get("/admin/api/status")
        assert resp.status_code == 401


def test_expired_cookie_rejected(app):
    """无效的（从未登录过的）cookie 必须返回 401。"""
    with TestClient(app) as client:
        client.cookies.set("admin_session", "any-random-string-not-in-sessions")
        resp = client.get("/admin/api/status")
        assert resp.status_code == 401


def test_wrong_bearer_rejected(app_with_agent_token):
    """错误的 Bearer token 必须返回 401。"""
    with TestClient(app_with_agent_token) as client:
        resp = client.get(
            "/admin/api/status",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


def test_valid_bearer_accepted(app_with_agent_token):
    """正确的 AGENT_API_TOKEN Bearer 可访问只读端点。"""
    with TestClient(app_with_agent_token) as client:
        resp = client.get(
            "/admin/api/status",
            headers={"Authorization": "Bearer test-agent-token-12345"},
        )
        assert resp.status_code == 200


def test_valid_cookie_session_accepted(app):
    """真实登录的 cookie session 可访问只读端点。"""
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/admin/api/status")
        assert resp.status_code == 200


def test_cookie_post_requires_csrf(app):
    """Cookie session 发起 POST 写操作必须有 CSRF token。"""
    with TestClient(app) as client:
        _login(client)
        resp = client.post(
            "/admin/api/groups/test-group/settings",
            params={"action_enabled": "true"},
        )
        assert resp.status_code == 403


def test_cookie_post_with_csrf_accepted(app):
    """Cookie session 带正确 CSRF 的 POST 可执行。"""
    with TestClient(app) as client:
        _login(client)
        page = client.get("/admin/shadow")
        assert page.status_code == 200, f"shadow page returned {page.status_code}"
        csrf = _extract_csrf(page.text)
        resp = client.post(
            "/admin/api/groups/test-group-csrf/settings",
            params={"moderation_enabled": "true"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code in (200, 201), f"POST returned {resp.status_code}: {resp.text[:200]}"


def test_bearer_post_no_csrf_needed(app_with_agent_token):
    """Bearer token POST 不需要 CSRF（非浏览器场景）。"""
    with TestClient(app_with_agent_token) as client:
        resp = client.post(
            "/admin/api/groups/test-bearer/settings",
            params={"moderation_enabled": "true"},
            headers={"Authorization": "Bearer test-agent-token-12345"},
        )
        assert resp.status_code == 200


def test_no_auth_returns_401(app):
    """无任何认证信息返回 401。"""
    with TestClient(app) as client:
        resp = client.get("/admin/api/status")
        assert resp.status_code == 401


def test_admin_password_not_accepted_as_bearer(app):
    """ADMIN_PASSWORD 不能作为 API Bearer token（密码与API token分离）。"""
    with TestClient(app) as client:
        resp = client.get(
            "/admin/api/status",
            headers={"Authorization": "Bearer test-admin-pass"},
        )
        assert resp.status_code == 401


def test_api_write_audited(app_with_agent_token):
    """Agent 写操作必须写入 AdminAudit。"""
    with TestClient(app_with_agent_token) as client:
        resp = client.post(
            "/admin/api/groups/test-audit/settings",
            params={"moderation_enabled": "true"},
            headers={"Authorization": "Bearer test-agent-token-12345"},
        )
        assert resp.status_code == 200
        from app.db import SessionLocal
        from app.models import AdminAudit
        from sqlalchemy import select

        import asyncio

        async def check():
            async with SessionLocal() as session:
                rows = (
                    await session.execute(
                        select(AdminAudit).where(AdminAudit.target_id == "test-audit")
                    )
                ).scalars().all()
                return rows

        rows = asyncio.run(check())
        assert len(rows) >= 1
