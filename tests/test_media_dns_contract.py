"""DNS checks use the same answer as the connection, with no real network I/O."""

import asyncio
import ipaddress
import socket
from pathlib import Path

import httpx
import pytest
from app.adapters.qq_official import media

FAKE_IP = "198.18.0.58"
PUBLIC_IP = "93.184.216.34"
CDN = "gchat.qpic.cn"
PAYLOAD = b"\xff\xd8\xffsynthetic-image"


def patch_dns(monkeypatch: pytest.MonkeyPatch, answers: list[list[str]]) -> list[str]:
    """Stub only the OS boundary; keep all application validators real."""
    calls: list[str] = []

    async def getaddrinfo(host, port, **kwargs):
        answer = answers[min(len(calls), len(answers) - 1)]
        calls.append(host)
        return [
            (
                socket.AF_INET6 if ipaddress.ip_address(ip).version == 6 else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, 0),
            )
            for ip in answer
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    return calls


async def download(tmp_path: Path, url: str, redirect: str | None = None):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if redirect and len(requests) == 1:
            return httpx.Response(302, headers={"Location": redirect})
        return httpx.Response(200, content=PAYLOAD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        result = await media.download_attachment(
            client, url, tmp_path, "synthetic", 0, "image/jpeg"
        )
    return result, requests


@pytest.mark.parametrize("unsafe", ["10.10.10.10", "127.0.0.1", "169.254.169.254", "::1"])
async def test_rejected_dns_answer_cannot_be_reclassified_by_second_lookup(
    tmp_path, monkeypatch, unsafe
):
    calls = patch_dns(monkeypatch, [[unsafe], [FAKE_IP]])
    result, requests = await download(tmp_path, f"https://{CDN}/image")
    assert result[0] is None
    assert "SSRF" in result[2]
    assert requests == []
    assert len(calls) == 1


async def test_fake_ip_connection_is_pinned_to_the_checked_answer(tmp_path, monkeypatch):
    calls = patch_dns(monkeypatch, [[FAKE_IP], ["127.0.0.1"]])
    result, requests = await download(tmp_path, f"https://{CDN}/image")
    assert result[0] is not None, result[2]
    assert (tmp_path / result[0]).read_bytes() == PAYLOAD
    assert len(calls) == 1
    assert requests[0].url.host == FAKE_IP
    assert requests[0].headers["host"] == CDN
    assert requests[0].extensions["sni_hostname"] == CDN


@pytest.mark.parametrize(
    "addresses",
    [[FAKE_IP, PUBLIC_IP], [PUBLIC_IP, FAKE_IP], [FAKE_IP, "10.10.10.10"]],
)
async def test_mixed_dns_answer_is_rejected_as_a_whole(tmp_path, monkeypatch, addresses):
    calls = patch_dns(monkeypatch, [addresses])
    result, requests = await download(tmp_path, f"https://{CDN}/image")
    assert result[0] is None
    assert requests == []
    assert len(calls) == 1


@pytest.mark.parametrize("addresses", [[FAKE_IP, PUBLIC_IP], [PUBLIC_IP, FAKE_IP]])
async def test_reasoned_resolver_does_not_drop_fake_ip_from_mixed_answer(monkeypatch, addresses):
    patch_dns(monkeypatch, [addresses])
    assert await media._resolve_addresses_reasoned(CDN) == ([], "unsafe")


@pytest.mark.parametrize("host", ["news.qq.com", "sub.qq.com.cn", "img.gchat.qpic.cn", "qq.com"])
async def test_fake_ip_exception_is_only_for_explicit_cdn_hosts(tmp_path, monkeypatch, host):
    patch_dns(monkeypatch, [[FAKE_IP]])
    result, requests = await download(tmp_path, f"https://{host}/image")
    assert result[0] is None
    assert requests == []


@pytest.mark.parametrize(
    "redirect",
    [
        f"http://{CDN}:8088/private",
        f"https://synthetic:password@{CDN}/private",
        f"ftp://{CDN}/private",
        "http://127.0.0.1/private",
        f"https://{CDN}.evil.example/private",
    ],
)
async def test_redirect_rechecks_url_and_cdn_boundary(tmp_path, monkeypatch, redirect):
    patch_dns(monkeypatch, [[PUBLIC_IP], [FAKE_IP]])
    result, requests = await download(tmp_path, "https://public.example/start", redirect)
    assert result[0] is None
    assert "SSRF" in result[2]
    assert len(requests) == 1


async def test_public_connection_remains_pinned_with_host_and_sni(tmp_path, monkeypatch):
    calls = patch_dns(monkeypatch, [[PUBLIC_IP], ["127.0.0.1"]])
    result, requests = await download(tmp_path, f"https://{CDN}/image")
    assert result[0] is not None
    assert len(calls) == 1
    assert requests[0].url.host == PUBLIC_IP
    assert requests[0].headers["host"] == CDN
    assert requests[0].extensions["sni_hostname"] == CDN


@pytest.mark.parametrize("failure", [socket.gaierror, OSError, TimeoutError])
async def test_dns_failure_is_not_retried_as_a_compatibility_exception(
    tmp_path, monkeypatch, failure
):
    calls = 0

    async def getaddrinfo(host, port, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure("synthetic DNS failure")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (FAKE_IP, 0))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    result, requests = await download(tmp_path, f"https://{CDN}/image")
    assert result[0] is None
    assert "SSRF" in result[2]
    assert requests == []
    assert calls == 1


async def test_empty_dns_answer_is_rejected_without_transport(tmp_path, monkeypatch):
    calls = patch_dns(monkeypatch, [[]])
    result, requests = await download(tmp_path, f"https://{CDN}/image")
    assert result[0] is None
    assert requests == []
    assert len(calls) == 1


async def test_pure_fake_ip_answer_retains_addresses_for_pinning(monkeypatch):
    patch_dns(monkeypatch, [[FAKE_IP, "198.19.0.1", FAKE_IP]])
    assert await media._resolve_addresses_reasoned(CDN) == ([FAKE_IP, "198.19.0.1"], "fake_ip")
