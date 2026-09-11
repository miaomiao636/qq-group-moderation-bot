"""R-107 regression: QQ official CDN under Fake-IP proxy DNS."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.adapters.qq_official import media as media_mod
from app.adapters.qq_official.media import (
    _is_fake_ip,
    _is_trusted_media_host,
    _stream_download,
)

FAKE_IP = "198.18.0.58"
PUBLIC_IP = "93.184.216.34"
PAYLOAD = b"\xff\xd8\xff" + b"x" * 128


@pytest.mark.parametrize(
    "host,expected",
    [
        ("multimedia.nt.qq.com.cn", True),
        ("MULTIMEDIA.NT.QQ.COM.CN.", True),
        ("img.gchat.qpic.cn", True),
        ("group.e.qq.com", True),
        ("news.qq.com", True),
        ("sub.qq.com.cn", True),
        ("evil.com", False),
        ("multimedia.nt.qq.com.cn.evil.com", False),
        ("qq.com.cn.evil.com", False),
        ("fakk-qq.com.cn", False),
    ],
)
def test_trusted_media_host_detection(host: str, expected: bool) -> None:
    assert _is_trusted_media_host(host) is expected


@pytest.mark.parametrize(
    "ip,expected",
    [(FAKE_IP, True), ("198.19.0.1", True), ("198.20.0.1", False), (PUBLIC_IP, False)],
)
def test_fake_ip_detection(ip: str, expected: bool) -> None:
    assert _is_fake_ip(ip) is expected


async def _fake_resolve(host: str) -> list[str]:
    # 真实 _resolve_public_addresses 会过滤不安全地址：Fake-IP 段被过滤后为空
    return []


async def _download(tmp_path: Path, url: str, monkeypatch: pytest.MonkeyPatch) -> tuple[bool, str]:
    monkeypatch.setattr(media_mod, "_resolve_public_addresses", _fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PAYLOAD)

    dest = tmp_path / "out.jpg"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        ok, reason, _bytes = await _stream_download(
            client,
            url,
            dest,
            size_limit=1024 * 1024,
            media_dir=tmp_path,
            quota_bytes=10 * 1024 * 1024,
        )
    return ok, reason


@pytest.mark.asyncio
async def test_whitelist_download_succeeds_under_fakeip_dns(tmp_path, monkeypatch) -> None:
    """官方 CDN 域名 + Fake-IP DNS：跳过 IP 锁定按域名连接，下载成功。"""
    ok, reason = await _download(tmp_path, "https://multimedia.nt.qq.com.cn/a.jpg", monkeypatch)
    assert ok, reason
    assert (tmp_path / "out.jpg").read_bytes() == PAYLOAD


@pytest.mark.asyncio
async def test_non_whitelist_fakeip_still_rejected(tmp_path, monkeypatch) -> None:
    """非白名单域名解析到 Fake-IP 仍被拒绝（SSRF 防护不放松）。"""
    ok, reason = await _download(tmp_path, "https://evil.example.com/a.jpg", monkeypatch)
    assert not ok
    assert "SSRF" in reason


@pytest.mark.asyncio
async def test_public_ip_pinned_download_unaffected(tmp_path, monkeypatch) -> None:
    """回归: 真实公网 IP 的锁定直连路径不受白名单改动影响。"""

    async def _public_resolve(host: str) -> list[str]:
        return [PUBLIC_IP]

    monkeypatch.setattr(media_mod, "_resolve_public_addresses", _public_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == PUBLIC_IP
        assert request.headers["Host"] == "multimedia.nt.qq.com.cn"
        return httpx.Response(200, content=PAYLOAD)

    dest = tmp_path / "out.jpg"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        ok, reason, _bytes = await _stream_download(
            client,
            "https://multimedia.nt.qq.com.cn/a.jpg",
            dest,
            size_limit=1024 * 1024,
            media_dir=tmp_path,
            quota_bytes=10 * 1024 * 1024,
        )
    assert ok, reason
    assert (tmp_path / "out.jpg").read_bytes() == PAYLOAD
