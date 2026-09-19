"""图片感知哈希白名单回归（负责人 2026-09-19）。

覆盖：dHash 稳定性（同图一致、重压缩/缩放后仍接近）、不同图案可区分、
十六进制往返、最近邻匹配与阈值、白名单读取 **fail-closed**、命中计数。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from app.db import SessionLocal
from app.models import ImageAllowlist
from app.moderation import image_hash
from PIL import Image
from sqlalchemy import delete, select


def _image_bytes(pattern: int) -> bytes:
    """生成可区分的测试图（0=左右分块，1=上下分块，2=渐变）。"""
    image = Image.new("L", (64, 64))
    pixels = image.load()
    for x in range(64):
        for y in range(64):
            if pattern == 0:
                pixels[x, y] = 0 if x < 32 else 255
            elif pattern == 1:
                pixels[x, y] = 255 if y < 32 else 0
            else:
                pixels[x, y] = (x * 4 + y * 3) % 256
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _recompressed(data: bytes, *, size: tuple[int, int] = (48, 48), quality: int = 60) -> bytes:
    """模拟 QQ 转发时的重压缩/缩放。"""
    with Image.open(io.BytesIO(data)) as image:
        resized = image.convert("RGB").resize(size)
        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()


def test_dhash_is_stable_for_same_bytes() -> None:
    data = _image_bytes(0)
    assert image_hash.dhash64(data) == image_hash.dhash64(data)


def test_dhash_survives_recompression_and_resize() -> None:
    data = _image_bytes(0)
    original = image_hash.dhash64(data)
    forwarded = image_hash.dhash64(_recompressed(data))
    assert original is not None and forwarded is not None
    assert image_hash.hamming64(original, forwarded) <= image_hash.DEFAULT_MAX_DISTANCE


def test_dhash_separates_different_patterns() -> None:
    left_right = image_hash.dhash64(_image_bytes(0))
    gradient = image_hash.dhash64(_image_bytes(2))
    assert left_right is not None and gradient is not None
    assert left_right != gradient
    # 不同图案不应被默认阈值判为同图
    assert image_hash.best_match(gradient, [(1, left_right)]) is None


@pytest.mark.parametrize("value", ["0", "ffffffffffffffff", "0123456789abcdef"])
def test_hex_roundtrip(value: str) -> None:
    parsed = image_hash.from_hex(value)
    assert parsed is not None
    assert image_hash.to_hex(parsed) == value.zfill(16)


@pytest.mark.parametrize("value", ["", "zzz", "ffffffffffffffffff"])
def test_hex_rejects_invalid(value: str) -> None:
    assert image_hash.from_hex(value) is None


def test_best_match_picks_nearest_within_threshold() -> None:
    target = image_hash.dhash64(_image_bytes(0))
    assert target is not None
    near = target
    far = image_hash.dhash64(_image_bytes(1))
    assert far is not None
    assert image_hash.best_match(target, [(7, far), (9, near)]) == (9, 0)
    assert image_hash.best_match(target, [(7, far)], max_distance=1) is None


def test_dhash_returns_none_for_undecodable_bytes() -> None:
    assert image_hash.dhash64(b"not an image") is None
    assert image_hash.dhash64_file(Path("definitely-not-here.png")) is None


class _BrokenSession:
    async def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("synthetic database failure")


async def test_load_enabled_is_fail_closed() -> None:
    """读不到白名单时必须返回空集（绝不误放行）。"""
    assert await image_hash.load_enabled(_BrokenSession()) == []  # type: ignore[arg-type]


@pytest.fixture
async def clean_allowlist():
    async with SessionLocal() as session:
        await session.execute(delete(ImageAllowlist))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(ImageAllowlist))
        await session.commit()


async def test_match_bytes_hits_seeded_row_and_counts_hits(clean_allowlist) -> None:
    data = _image_bytes(0)
    phash = image_hash.dhash64(data)
    assert phash is not None
    async with SessionLocal() as session:
        session.add(
            ImageAllowlist(
                phash=image_hash.to_hex(phash),
                note="synthetic",
                source="test",
                created_by="synthetic",
            )
        )
        await session.commit()
        hit = await image_hash.match_bytes(session, data)
        assert hit is not None and hit[1] == 0
        await image_hash.record_hit(session, hit[0])
        await session.commit()
        row = (await session.execute(select(ImageAllowlist))).scalar_one()
        assert row.hit_count == 1


async def test_match_bytes_ignores_disabled_and_unknown(clean_allowlist) -> None:
    data = _image_bytes(0)
    phash = image_hash.dhash64(data)
    assert phash is not None
    async with SessionLocal() as session:
        session.add(
            ImageAllowlist(
                phash=image_hash.to_hex(phash),
                note="disabled",
                source="test",
                enabled=False,
                created_by="synthetic",
            )
        )
        await session.commit()
        assert await image_hash.match_bytes(session, data) is None
        assert await image_hash.match_bytes(session, _image_bytes(1)) is None
