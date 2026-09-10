"""Run once per minute under an independent Windows Scheduled Task.

This process cannot report its own machine losing power. Healthchecks must be
configured outside the machine to alert when these signals stop arriving.
"""

from __future__ import annotations

import asyncio
import json
from typing import Protocol

import httpx

from app.notifications.config import NotificationSettings
from app.notifications.contracts import DeliveryResult


class Heartbeat(Protocol):
    async def ping(self, healthy: bool) -> DeliveryResult: ...


async def run_probe(
    settings: NotificationSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    heartbeat: Heartbeat | None = None,
) -> int:
    if not settings.enabled or not settings.heartbeat_enabled:
        return 0
    healthy = False
    try:
        async with asyncio.timeout(6):
            async with httpx.AsyncClient(
                timeout=5, follow_redirects=False, trust_env=False, transport=transport
            ) as client:
                async with client.stream(
                    "GET",
                    f"http://127.0.0.1:{settings.heartbeat_local_port}/health/ready",
                    headers={"Authorization": f"Bearer {settings.heartbeat_probe_token}"},
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 8192:
                            raise ValueError("oversized readiness response")
                    payload = json.loads(body)
                    checks = payload.get("checks") if isinstance(payload, dict) else None
                    healthy = (
                        response.status_code == 200
                        and isinstance(payload, dict)
                        and payload.get("ready") is True
                        and isinstance(checks, dict)
                        and all(
                            checks.get(key) is True
                            for key in (
                                "database",
                                "backlog",
                                "onebot",
                                "moderation_worker",
                                "notification_worker",
                                "notification_delivery",
                            )
                        )
                    )
    except (httpx.HTTPError, ValueError, TimeoutError):
        # Do not print exception messages: even localhost errors may contain tokens.
        healthy = False
    if heartbeat is None:
        from app.notifications.heartbeat import HeartbeatSender

        heartbeat = HeartbeatSender(settings)
    result = await heartbeat.ping(healthy)
    return 0 if healthy and result.status == "SENT" else 1


def main() -> int:
    try:
        settings = NotificationSettings()
        code = asyncio.run(run_probe(settings))
    except Exception:  # noqa: BLE001 - CLI exposes a fixed operational code only
        print('{"status":"failed","error_code":"watchdog_failed"}')
        return 1
    status = (
        "skipped"
        if not settings.enabled or not settings.heartbeat_enabled
        else ("healthy" if code == 0 else "unhealthy_or_unsent")
    )
    print(json.dumps({"status": status}, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
