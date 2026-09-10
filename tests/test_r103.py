"""R-103 correctness regressions."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import httpx
import pytest
from app.adapters.qq_official.dedup import begin_processing, reset_memory_cache
from app.adapters.qq_official.media import download_attachment, safe_filename, total_media_size
from app.db import SessionLocal
from app.runtime.pipeline import run_pipeline


class _FakeByteStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.headers: dict[str, str] = {}
        self.status_code = 200

    async def __aenter__(self) -> _FakeByteStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        del chunk_size
        for chunk in self._chunks:
            yield chunk


class _FakeStreamClient:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def stream(self, *_args: object, **_kwargs: object) -> _FakeByteStream:
        return _FakeByteStream(self._chunks)


@pytest.fixture(autouse=True)
def _clean_dedup_cache() -> Generator[None, None, None]:
    reset_memory_cache()
    yield
    reset_memory_cache()


@pytest.mark.asyncio
async def test_processing_lease_blocks_second_claim() -> None:
    mid = f"LEASE_{uuid.uuid4().hex}"
    async with SessionLocal() as s1, SessionLocal() as s2:
        first = await begin_processing(s1, mid, lease_seconds=300)
        second = await begin_processing(s2, mid, lease_seconds=300)

    assert first.accepted is True
    assert first.token
    assert first.status == "PROCESSING"
    assert second.accepted is False
    assert second.status == "PROCESSING"


@pytest.mark.asyncio
async def test_permanent_parse_error_is_not_retried_or_duplicated() -> None:
    payload = {
        "id": f"BAD_{uuid.uuid4().hex}",
        "author": {"member_openid": "M_BAD", "username": "bad"},
    }
    async with SessionLocal() as session:
        first = await run_pipeline(payload, session)
        second = await run_pipeline(payload, session)

    assert first is not None
    assert first.verdict == "record_only"
    assert second is None


def test_safe_filename_uses_full_message_identity() -> None:
    same_tail = "X" * 24
    a = "A" * 40 + same_tail
    b = "B" * 40 + same_tail

    assert safe_filename(a, 0) != safe_filename(b, 0)


@pytest.mark.asyncio
async def test_download_enforces_quota_during_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "existing.bin").write_bytes(b"12345678")
    client = _FakeStreamClient([b"12345"])
    # P1-6: bypass SSRF check for this unit test (tests quota logic, not SSRF)
    from app.adapters.qq_official import media as media_mod

    monkeypatch.setattr(media_mod, "_pin_media_url", _fixed_test_destination)

    name, _ext, reason = await download_attachment(
        client,  # type: ignore[arg-type]
        "https://example.invalid/a",
        tmp_path,
        "MSG",
        0,
        "image/jpeg",
        quota_bytes=10,
    )

    assert name is None
    assert "配额" in reason
    assert total_media_size(tmp_path) == 8


async def _fixed_test_destination(url: str):
    import httpx

    return httpx.URL("https://93.184.216.34/a"), "example.invalid", "example.invalid"


@pytest.mark.asyncio
async def test_download_reads_only_magic_header_for_sniffing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"x" * 1024))
    )
    try:
        called = False

        def forbidden_read_bytes(self: Path) -> bytes:
            nonlocal called
            called = True
            raise AssertionError("read_bytes must not be used for sniffing")

        monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
        from app.adapters.qq_official import media as media_mod

        monkeypatch.setattr(media_mod, "_pin_media_url", _fixed_test_destination)
        name, _ext, reason = await download_attachment(
            client,
            "https://example.invalid/a",
            tmp_path,
            "MSG",
            0,
            "image/jpeg",
            quota_bytes=2048,
        )
    finally:
        await client.aclose()

    assert name is not None
    assert reason == ""
    assert called is False
