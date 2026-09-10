"""External pings need authenticated application readiness, not a 200 alone."""

import httpx
import pytest
from app.notifications.config import NotificationSettings
from app.notifications.contracts import DeliveryResult
from app.notifications.watchdog import run_probe


class FakeHeartbeat:
    def __init__(self):
        self.calls = []

    async def ping(self, healthy):
        self.calls.append(healthy)
        return DeliveryResult("SENT")


def settings():
    return NotificationSettings(
        _env_file=None,
        enabled=True,
        heartbeat_enabled=True,
        heartbeat_url="https://hc-ping.com/00000000-0000-4000-8000-000000000000",
        heartbeat_probe_token="synthetic-local-probe-token-for-test",
        heartbeat_local_port=8123,
    )


@pytest.mark.asyncio
async def test_disabled_probe_performs_zero_local_or_remote_io():
    heartbeat = FakeHeartbeat()

    def forbidden(request):
        raise AssertionError("unexpected network request")

    assert (
        await run_probe(
            NotificationSettings(_env_file=None),
            transport=httpx.MockTransport(forbidden),
            heartbeat=heartbeat,
        )
        == 0
    )
    assert not heartbeat.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,status,healthy",
    [
        ({"status": "ok"}, 200, False),
        ([], 200, False),
        (None, 200, False),
        ({"ready": True}, 200, False),
        ({"ready": "true", "checks": {}}, 200, False),
        (
            {
                "ready": True,
                "checks": {
                    k: True
                    for k in (
                        "database",
                        "backlog",
                        "onebot",
                        "moderation_worker",
                        "notification_worker",
                        "notification_delivery",
                    )
                },
            },
            200,
            True,
        ),
        ({"ready": False}, 503, False),
    ],
)
async def test_readiness_contract_and_dedicated_header(payload, status, healthy):
    heartbeat = FakeHeartbeat()

    def handler(request):
        assert str(request.url) == "http://127.0.0.1:8123/health/ready"
        assert request.headers["authorization"] == "Bearer synthetic-local-probe-token-for-test"
        return httpx.Response(status, json=payload)

    result = await run_probe(
        settings(), transport=httpx.MockTransport(handler), heartbeat=heartbeat
    )
    assert heartbeat.calls == [healthy]
    assert result == (0 if healthy else 1)


@pytest.mark.asyncio
async def test_refused_connection_signals_failure_without_exception_details():
    heartbeat = FakeHeartbeat()

    def handler(request):
        raise httpx.ConnectError("private connection details", request=request)

    assert (
        await run_probe(settings(), transport=httpx.MockTransport(handler), heartbeat=heartbeat)
        == 1
    )
    assert heartbeat.calls == [False]


@pytest.mark.asyncio
async def test_redirect_never_forwards_probe_token():
    heartbeat = FakeHeartbeat()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://untrusted.invalid"})

    assert (
        await run_probe(settings(), transport=httpx.MockTransport(handler), heartbeat=heartbeat)
        == 1
    )
    assert len(calls) == 1 and heartbeat.calls == [False]
