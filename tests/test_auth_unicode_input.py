"""Non-ASCII input must not turn authentication rejection into a server error."""

import pytest
from app.config import get_settings
from app.web import auth, confirm
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def auth_app(monkeypatch):
    from app.main import app

    monkeypatch.setenv("AGENT_API_TOKEN", "synthetic-unicode-writer")
    monkeypatch.setenv("AGENT_API_READ_TOKEN", "synthetic-unicode-reader")
    get_settings.cache_clear()
    yield app
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "username,password",
    [("不存在的用户", "wrong"), ("admin", "错误密码"), ("admin", "🔒wrong")],
)
def test_wrong_unicode_login_returns_rejection(auth_app, username, password):
    with TestClient(auth_app) as client:
        response = client.post(
            "/admin/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/admin/login?error=")
        assert auth.SESSION_COOKIE not in client.cookies


@pytest.mark.parametrize(
    "username,password", [("管理员", "synthetic-password"), ("admin", "合成密码🔒")]
)
def test_configured_unicode_credentials_can_login(monkeypatch, username, password):
    monkeypatch.setenv("ADMIN_USERNAME", username)
    monkeypatch.setenv("ADMIN_PASSWORD", password)
    get_settings.cache_clear()
    token = None
    try:
        token = auth.login(username, password)
        assert auth.is_valid(token)
        with pytest.raises(auth.AuthError):
            auth.login(username, password + "错误")
    finally:
        if token:
            auth.logout(token)
        get_settings.cache_clear()


def test_unicode_csrf_is_rejected_without_consuming_session(auth_app):
    with TestClient(auth_app) as client:
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-admin-pass"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        token = client.cookies[auth.SESSION_COOKIE]
        response = client.post("/admin/emergency-stop", data={"csrf": "错误令牌"})
        assert response.status_code == 403
        assert auth.is_valid(token)
        assert auth.validate_csrf(token, auth.csrf_token(token))


@pytest.mark.parametrize("path", ["/admin/api/status", "/onebot/status"])
def test_non_ascii_bearer_is_rejected(auth_app, path):
    with TestClient(auth_app) as client:
        response = client.get(path, headers={b"authorization": b"Bearer \xff"})
        assert response.status_code == 401


def test_non_ascii_onebot_websocket_token_is_rejected(auth_app):
    with TestClient(auth_app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect(
                get_settings().onebot_ws_path,
                headers={b"authorization": b"Bearer \xff", "x-self-id": "10000001"},
            ),
        ):
            pytest.fail("Invalid credential must never establish a connection")
        assert rejected.value.code == 1008


def test_unicode_confirmation_code_is_rejected_without_consuming_code():
    case_id = 910000001
    code = confirm.generate(case_id)
    try:
        assert not confirm.verify_and_consume(case_id, "错误码")
        assert confirm.verify_and_consume(case_id, code)
    finally:
        confirm.clear(case_id)
