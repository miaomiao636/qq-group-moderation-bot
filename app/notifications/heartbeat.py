"""A fixed Healthchecks ping, never an account/check provisioning client.

Protocol: https://healthchecks.io/docs/http_api/
Transport: https://www.python-httpx.org/advanced/transports/#custom-transports
"""

from __future__ import annotations

import asyncio

import httpx

from app.notifications.config import NotificationSettings
from app.notifications.contracts import DeliveryResult


class HeartbeatSender:
    def __init__(
        self, settings: NotificationSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings, self._transport = settings, transport

    async def ping(self, healthy: bool) -> DeliveryResult:
        settings = self._settings
        if not settings.enabled or not settings.heartbeat_enabled:
            return DeliveryResult("SKIPPED", "heartbeat_disabled")
        if type(healthy) is not bool:
            return DeliveryResult("FAILED", "heartbeat_invalid_signal")
        # Calling the transport directly avoids AsyncClient's INFO log containing
        # the credential URL. It also has no redirect/proxy/cookie processing.
        # The configured UUID is never included in error codes or request bodies.
        request = httpx.Request(
            "GET",
            settings.heartbeat_url + ("" if healthy else "/fail"),
            extensions={
                "timeout": {
                    name: settings.timeout_seconds for name in ("connect", "read", "write", "pool")
                }
            },
        )
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                transport = self._transport or httpx.AsyncHTTPTransport(
                    trust_env=False,
                    verify=True,
                    retries=0,
                    http2=False,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                )
                async with transport:
                    response = await transport.handle_async_request(request)
                    try:
                        if 300 <= response.status_code < 500:
                            return DeliveryResult("FAILED", "heartbeat_rejected")
                        if response.status_code != 200:
                            return DeliveryResult("UNKNOWN", "heartbeat_response_uncertain")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > 128:
                                return DeliveryResult("UNKNOWN", "heartbeat_response_uncertain")
                            body.extend(chunk)
                        # UUID endpoints also use HTTP 200 for not-found/rate-limit.
                        if bytes(body) in (b"OK (not found)", b"OK (rate limited)"):
                            return DeliveryResult("FAILED", "heartbeat_not_accepted")
                        if bytes(body) != b"OK":
                            return DeliveryResult("UNKNOWN", "heartbeat_response_uncertain")
                        return DeliveryResult("SENT")
                    finally:
                        await response.aclose()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return DeliveryResult("FAILED", "heartbeat_connect_failed", retryable=True)
        except Exception:
            return DeliveryResult("UNKNOWN", "heartbeat_transport_uncertain")
