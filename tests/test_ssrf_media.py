"""P1-6: SSRF DNS resolution + redirect hardening tests."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.adapters.qq_official.media import (
    _check_dns_resolution,
    _is_safe_ip,
    _is_safe_media_url,
)


def test_literal_loopback_ip_rejected() -> None:
    assert not asyncio.run(_is_safe_media_url("http://127.0.0.1/x.jpg"))


def test_literal_private_ip_rejected() -> None:
    assert not asyncio.run(_is_safe_media_url("http://10.0.0.1/x.jpg"))
    assert not asyncio.run(_is_safe_media_url("http://172.16.0.1/x.jpg"))
    assert not asyncio.run(_is_safe_media_url("http://192.168.1.1/x.jpg"))


def test_ipv6_loopback_rejected() -> None:
    assert not asyncio.run(_is_safe_media_url("http://[::1]/x.jpg"))


def test_ipv6_link_local_rejected() -> None:
    assert not asyncio.run(_is_safe_media_url("http://[fe80::1]/x.jpg"))


def test_non_http_scheme_rejected() -> None:
    assert not asyncio.run(_is_safe_media_url("ftp://example.com/x.jpg"))
    assert not asyncio.run(_is_safe_media_url("file:///etc/passwd"))


def test_non_standard_port_rejected() -> None:
    assert not asyncio.run(_is_safe_media_url("http://example.com:8080/x.jpg"))
    assert not asyncio.run(_is_safe_media_url("http://example.com:22/x.jpg"))


def test_standard_port_accepted() -> None:
    assert asyncio.run(_is_safe_media_url("https://93.184.216.34/x.jpg"))
    assert asyncio.run(_is_safe_media_url("http://93.184.216.34/x.jpg"))


def test_no_port_specified_accepted() -> None:
    assert asyncio.run(_is_safe_media_url("https://93.184.216.34/media.jpg"))


def test_localhost_hostname_rejected() -> None:
    assert not asyncio.run(_is_safe_media_url("http://localhost/x.jpg"))


def test_domain_resolving_to_loopback_rejected() -> None:
    """Domain that resolves to 127.0.0.1 must be rejected (DNS-based SSRF)."""

    async def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.getaddrinfo = fake_getaddrinfo
        result = asyncio.run(_check_dns_resolution("evil.example.com"))
    assert result is False


def test_domain_resolving_to_private_rejected() -> None:
    async def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("192.168.1.100", 0))]

    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.getaddrinfo = fake_getaddrinfo
        result = asyncio.run(_check_dns_resolution("internal.example.com"))
    assert result is False


def test_domain_resolving_to_public_accepted() -> None:
    async def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.getaddrinfo = fake_getaddrinfo
        result = asyncio.run(_check_dns_resolution("cdn.qq.com"))
    assert result is True


def test_domain_resolving_to_ipv6_loopback_rejected() -> None:
    async def fake_getaddrinfo(host, port, **kw):
        return [(10, 1, 6, "", ("::1", 0))]

    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.getaddrinfo = fake_getaddrinfo
        result = asyncio.run(_check_dns_resolution("v6.example.com"))
    assert result is False


def test_dns_failure_rejected() -> None:
    """Unresolvable domain must be rejected (fail-closed)."""
    import socket

    async def fake_getaddrinfo(host, port, **kw):
        raise socket.gaierror("resolution failed")

    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.getaddrinfo = fake_getaddrinfo
        result = asyncio.run(_check_dns_resolution("nonexistent.example.com"))
    assert result is False


def test_is_safe_ip_helper() -> None:
    import ipaddress

    assert _is_safe_ip(ipaddress.ip_address("8.8.8.8")) is True
    assert _is_safe_ip(ipaddress.ip_address("127.0.0.1")) is False
    assert _is_safe_ip(ipaddress.ip_address("169.254.1.1")) is False
    assert _is_safe_ip(ipaddress.ip_address("::1")) is False
    assert _is_safe_ip(ipaddress.ip_address("fe80::1")) is False
    assert _is_safe_ip(ipaddress.ip_address("0.0.0.0")) is False


def test_download_attachment_rejects_unsafe_url() -> None:
    """Full download path rejects SSRF URLs."""
    import tempfile
    from pathlib import Path

    import httpx
    from app.adapters.qq_official.media import download_attachment

    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            client = httpx.AsyncClient()
            try:
                name, ext, reason = await download_attachment(
                    client,
                    "http://127.0.0.1/steal.jpg",
                    Path(tmp),
                    "msg_test",
                    0,
                    "image/jpeg",
                )
                return name, reason
            finally:
                await client.aclose()

    name, reason = asyncio.run(run())
    assert name is None
    assert "SSRF" in reason
