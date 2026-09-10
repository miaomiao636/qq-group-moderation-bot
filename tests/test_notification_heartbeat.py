"""Healthchecks UUIDs are secrets; no ping is ever sent outside MockTransport."""

import asyncio
import logging

import httpx
import pytest
from app.notifications.config import NotificationSettings
from app.notifications.heartbeat import HeartbeatSender

URL = "https://hc-ping.com/11111111-1111-4111-8111-111111111111"


def heartbeat_settings():
    return NotificationSettings(
        _env_file=None,
        enabled=True,
        heartbeat_enabled=True,
        heartbeat_url=URL,
        heartbeat_probe_token="fixture-probe-token-not-a-real-key-32",
    )


async def test_disabled_heartbeat_never_contacts_external_service():
    def forbidden(_request):
        pytest.fail("disabled heartbeat called transport")

    result = await HeartbeatSender(
        NotificationSettings(_env_file=None), transport=httpx.MockTransport(forbidden)
    ).ping(True)
    assert result.status == "SKIPPED"


@pytest.mark.parametrize("healthy,suffix", [(True, ""), (False, "/fail")])
async def test_heartbeat_fixed_signal_without_business_payload_or_secret_logs(
    healthy, suffix, caplog
):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b"OK")

    with caplog.at_level(logging.DEBUG):
        result = await HeartbeatSender(
            heartbeat_settings(), transport=httpx.MockTransport(handle)
        ).ping(healthy)
    assert result.status == "SENT"
    assert len(requests) == 1 and str(requests[0].url) == URL + suffix
    assert requests[0].method == "GET" and requests[0].content == b""
    assert requests[0].extensions["timeout"] == {
        name: 8 for name in ("connect", "read", "write", "pool")
    }
    assert "11111111-1111" not in caplog.text
    assert "fixture-probe" not in repr(requests[0].headers)


async def test_heartbeat_does_not_follow_redirect_to_another_host():
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    result = await HeartbeatSender(
        heartbeat_settings(), transport=httpx.MockTransport(redirect)
    ).ping(True)
    assert result.status == "FAILED" and calls == [URL]


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, b"OK", "SENT"),
        (200, b"OK (not found)", "FAILED"),
        (200, b"OK (rate limited)", "FAILED"),
        (200, b"unexpected", "UNKNOWN"),
        (200, b"x" * 1024, "UNKNOWN"),
        (400, b"invalid", "FAILED"),
        (500, b"failure", "UNKNOWN"),
    ],
)
async def test_heartbeat_success_requires_unambiguous_acceptance(status, body, expected):
    result = await HeartbeatSender(
        heartbeat_settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body)),
    ).ping(True)
    assert result.status == expected and not result.retryable


@pytest.mark.parametrize(
    "error,status,retryable",
    [
        (httpx.ConnectError, "FAILED", True),
        (httpx.ConnectTimeout, "FAILED", True),
        (httpx.ReadTimeout, "UNKNOWN", False),
        (httpx.WriteError, "UNKNOWN", False),
    ],
)
async def test_heartbeat_error_does_not_expose_secret_url(error, status, retryable, caplog):
    def fail(request):
        raise error(URL, request=request)

    result = await HeartbeatSender(heartbeat_settings(), transport=httpx.MockTransport(fail)).ping(
        True
    )
    assert (result.status, result.retryable) == (status, retryable)
    assert URL not in repr(result) + caplog.text


async def test_heartbeat_total_deadline_includes_body_wait():
    async def hang(_request):
        await asyncio.Event().wait()

    config = heartbeat_settings().model_copy(update={"timeout_seconds": 0.02})
    result = await HeartbeatSender(config, transport=httpx.MockTransport(hang)).ping(True)
    assert result.status == "UNKNOWN" and not result.retryable
