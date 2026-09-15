"""R-107 / R01 regression: QQ official CDN under Fake-IP proxy DNS.

R01（ad323b6 主审）：兼容出口只允许「受信 CDN 域名 + DNS 全部解析为 Fake-IP」；
私网/回环/链路本地等 unsafe 解析、DNS 失败、重定向到非常规端口一律拒绝。
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
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
        ("gchat.qpic.cn", True),
        ("img.gchat.qpic.cn", False),
        ("group.e.qq.com", True),
        ("news.qq.com", False),
        ("sub.qq.com.cn", False),
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


async def _download(tmp_path: Path, url: str, monkeypatch: pytest.MonkeyPatch) -> tuple[bool, str]:
    return (await _download_follow(tmp_path, url, monkeypatch, follow=None))[:2]


async def _download_follow(
    tmp_path: Path,
    url: str,
    monkeypatch: pytest.MonkeyPatch,
    follow: str | None,
    hops: list[str] | None = None,
) -> tuple[bool, str, list[str]]:
    seen = hops if hops is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if follow and len(seen) == 1:
            return httpx.Response(302, headers={"Location": follow})
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
    return ok, reason, seen


def _patch_reason(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    """Stub actual DNS answers, not application security decisions."""
    answers = {
        "fake_ip": [FAKE_IP],
        "unsafe": ["10.10.10.10"],
        "mixed": [FAKE_IP, "10.10.10.10"],
        "public": [PUBLIC_IP],
    }

    async def getaddrinfo(host, port, **kwargs):
        if reason in ("dns", "url"):
            raise socket.gaierror("synthetic DNS failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answers[reason]]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)


@pytest.mark.asyncio
async def test_whitelist_download_succeeds_under_fakeip_dns(tmp_path, monkeypatch) -> None:
    """官方 CDN + 全 Fake-IP：固定已验证地址，保留兼容出口。"""
    _patch_reason(monkeypatch, "fake_ip")
    ok, reason, _ = await _download_follow(
        tmp_path, "https://multimedia.nt.qq.com.cn/a.jpg", monkeypatch, None
    )
    assert ok, reason
    assert (tmp_path / "out.jpg").read_bytes() == PAYLOAD


# ---- R01 主审复现场景：兼容分支不得豁免其他拒绝原因 ----


@pytest.mark.parametrize("reason", ["unsafe", "dns", "url"])
@pytest.mark.asyncio
async def test_r01_whitelist_domain_with_unsafe_or_dns_failure_rejected(
    tmp_path, monkeypatch, reason: str
) -> None:
    """白名单域解析到私网/回环（unsafe）或 DNS 失败时，兼容出口必须关闭。"""
    _patch_reason(monkeypatch, reason)
    for host in (
        "multimedia.nt.qq.com.cn",
        "gchat.qpic.cn",
        "news.qq.com",
    ):
        ok, reason_out, _ = await _download_follow(
            tmp_path, f"https://{host}/a.jpg", monkeypatch, None
        )
        assert not ok, f"{host} reason={reason} 不应放行"
        assert "SSRF" in reason_out


@pytest.mark.asyncio
async def test_r01_mixed_fake_and_private_rejected(tmp_path, monkeypatch) -> None:
    """Fake-IP 与真实内网混杂解析：unsafe，不得豁免。"""

    # The old test mocked only the second lookup and accidentally used live DNS
    # for the first. CI resolved a public address and bypassed that mock entirely.
    _patch_reason(monkeypatch, "mixed")
    ok, reason, _ = await _download_follow(
        tmp_path, "https://multimedia.nt.qq.com.cn/a.jpg", monkeypatch, None
    )
    assert not ok
    assert "SSRF" in reason


@pytest.mark.asyncio
async def test_r01_redirect_to_whitelist_odd_port_rejected(tmp_path, monkeypatch) -> None:
    """重定向到白名单域名的 8088 端口：逐跳 sync 检查（端口白名单）必须拦截。"""
    _patch_reason(monkeypatch, "fake_ip")
    ok, reason, seen = await _download_follow(
        tmp_path,
        "https://multimedia.nt.qq.com.cn/start",
        monkeypatch,
        follow="http://multimedia.nt.qq.com.cn:8088/payload",
    )
    assert not ok
    assert "SSRF" in reason
    assert len(seen) == 1  # 第二跳未发出


@pytest.mark.asyncio
async def test_non_whitelist_fakeip_still_rejected(tmp_path, monkeypatch) -> None:
    """非白名单域名解析到 Fake-IP 仍被拒绝（SSRF 防护不放松）。"""
    _patch_reason(monkeypatch, "fake_ip")
    ok, reason, _ = await _download_follow(
        tmp_path, "https://evil.example.com/a.jpg", monkeypatch, None
    )
    assert not ok
    assert "SSRF" in reason


@pytest.mark.asyncio
async def test_public_ip_pinned_download_unaffected(tmp_path, monkeypatch) -> None:
    """回归: 真实公网 IP 的锁定直连路径不受白名单改动影响。"""

    _patch_reason(monkeypatch, "public")

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
