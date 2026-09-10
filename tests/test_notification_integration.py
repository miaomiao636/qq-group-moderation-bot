"""Safe defaults and the authenticated external readiness boundary."""

import httpx
import pytest
from app.main import create_app


@pytest.mark.asyncio
async def test_private_readiness_disabled_by_default():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        assert (await client.get("/health/ready")).status_code == 404


@pytest.mark.asyncio
async def test_probe_token_only_and_unhealthy_is_503(monkeypatch):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv(
        "NOTIFICATION_HEARTBEAT_URL", "https://hc-ping.com/00000000-0000-4000-8000-000000000000"
    )
    monkeypatch.setenv(
        "NOTIFICATION_HEARTBEAT_PROBE_TOKEN", "synthetic-probe-token-read-only-value"
    )
    from app.notifications import health

    async def probe():
        return {"ready": False, "checks": {"database": False}}

    monkeypatch.setattr(health, "check_readiness", probe)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        assert (await client.get("/health/ready")).status_code == 401
        assert (
            await client.get("/health/ready?token=synthetic-probe-token-read-only-value")
        ).status_code == 401
        response = await client.get(
            "/health/ready",
            headers={"Authorization": "Bearer synthetic-probe-token-read-only-value"},
        )
        assert response.status_code == 503 and response.json()["ready"] is False
        assert "token" not in response.text


@pytest.mark.asyncio
async def test_notification_page_is_registered():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        assert (await client.get("/admin/notifications")).status_code == 303


def test_health_probe_cannot_reuse_management_credentials(monkeypatch):
    from app import main
    from app.config import Settings

    token = "synthetic-probe-token-read-only-value"
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("NOTIFICATION_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv(
        "NOTIFICATION_HEARTBEAT_URL", "https://hc-ping.com/00000000-0000-4000-8000-000000000000"
    )
    monkeypatch.setenv("NOTIFICATION_HEARTBEAT_PROBE_TOKEN", token)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(_env_file=None, onebot_ws_enabled=False, agent_api_read_token=token),
    )
    with pytest.raises(ValueError, match="dedicated"):
        create_app()
