"""R-105: resolve once, pin the address, retain the logical HTTPS identity."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from app.adapters.qq_official import media


@pytest.mark.asyncio
async def test_dns_address_is_pinned_with_original_host_and_sni(tmp_path: Path) -> None:
    answers = []

    def resolver(host, port, *args, **kwargs):
        address = "93.184.216.34" if not answers else "127.0.0.1"
        answers.append(address)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "cdn.example.com"
        assert request.headers["connection"] == "close"
        assert request.extensions["sni_hostname"] == "cdn.example.com"
        return httpx.Response(200, content=b"sample")

    with patch("socket.getaddrinfo", resolver):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await media.stream_download(
                client, "https://cdn.example.com/image.jpg", tmp_path / "image"
            )
    assert result == (True, "", 6)
    assert answers == ["93.184.216.34"]


@pytest.mark.parametrize(
    "address", ["ff02::1", "2001:db8::1", "192.0.2.1", "100.64.0.1", "2002:a00:1::"]
)
def test_non_public_ranges_are_rejected(address: str) -> None:
    host = f"[{address}]" if ":" in address else address
    assert not asyncio.run(media._is_safe_media_url(f"https://{host}/image"))


@pytest.mark.asyncio
async def test_redirect_checks_destination_and_never_returns_signed_url(tmp_path: Path) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302, headers={"location": "http://127.0.0.1/private?token=not-a-secret"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok, reason, _ = await media.stream_download(
            client, "https://93.184.216.34/image", tmp_path / "image"
        )
    assert not ok and len(requests) == 1
    assert "token" not in reason and "127.0.0.1" not in reason


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example:bad/image",
        "http://[broken/image",
        "http://user:pass@93.184.216.34/image",
    ],
)
def test_malformed_or_credentialed_urls_are_rejected(url: str) -> None:
    assert not asyncio.run(media._is_safe_media_url(url))


@pytest.mark.asyncio
async def test_dns_mixed_public_and_private_answers_reject_the_whole_destination() -> None:
    answers = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
    ]
    with patch("socket.getaddrinfo", return_value=answers):
        assert not await media._is_safe_media_url("https://cdn.example.com/image")


@pytest.mark.asyncio
async def test_slow_stream_has_total_deadline_and_removes_partial_file(
    tmp_path, monkeypatch
) -> None:
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            await asyncio.Event().wait()

    monkeypatch.setattr(media, "_DOWNLOAD_TOTAL_SECONDS", 0.02, raising=False)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=SlowStream()))
    destination = tmp_path / "image"
    async with httpx.AsyncClient(transport=transport) as client:
        result = await asyncio.wait_for(
            media.stream_download(client, "https://93.184.216.34/image", destination), 0.2
        )
    assert not result[0] and "超时" in result[1]
    assert not destination.exists()
    assert not destination.with_suffix(".part").exists()
