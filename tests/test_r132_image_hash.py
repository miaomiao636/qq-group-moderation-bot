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


# --- 管线 shadow 接入（默认 off：线上行为零变化）-----------------------------------------


class _Attachment:
    def __init__(self, filename: str, content_type: str = "image/png") -> None:
        self.filename = filename
        self.content_type = content_type


async def test_observe_shadow_is_off_by_default(monkeypatch, tmp_path: Path) -> None:
    """``IMAGE_HASH_MODE`` 未设置 → 返回 None（调用方不写字段，行为零变化）。"""
    monkeypatch.delenv("IMAGE_HASH_MODE", raising=False)

    # 配置隔离：`.env` 由 pydantic-settings 装载，**部署时可能已配成 shadow**；
    # 本用例问的是"**未配置**时为 off"，因此把应用配置也显式置空（非改断言，只隔离输入）。
    class _NoMode:
        image_hash_mode = ""

    monkeypatch.setattr("app.config.get_settings", lambda: _NoMode())
    (tmp_path / "a.png").write_bytes(_image_bytes(0))
    async with SessionLocal() as session:
        assert (
            await image_hash.observe_shadow(
                session,
                attachments=[_Attachment("a.png")],
                media_dir=tmp_path,
                verdict="violation_high",
                category="ad",
                rule_ids=[],
            )
            is None
        )


async def test_observe_shadow_invalid_mode_falls_back_to_off(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("IMAGE_HASH_MODE", "enforce-please")
    assert image_hash.mode() == "off"


@pytest.mark.parametrize(
    "category,verdict,rule_ids,expected_would_allow,expected_blocked",
    [
        ("ad", "violation_high", [], True, ""),
        ("porn", "violation_high", [], False, "category"),
        ("ad", "violation_high", ["R003"], False, "hard_evidence"),
        ("ad", "allow", [], False, ""),
    ],
)
async def test_observe_shadow_records_hit_and_exceptions(
    monkeypatch,
    clean_allowlist,
    tmp_path: Path,
    category,
    verdict,
    rule_ids,
    expected_would_allow,
    expected_blocked,
) -> None:
    """命中记录 would_allow；色情/本地硬证据/已放行 均不记 would_allow。"""
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    data = _image_bytes(0)
    (tmp_path / "a.png").write_bytes(data)
    phash = image_hash.dhash64(data)
    assert phash is not None
    async with SessionLocal() as session:
        session.add(
            ImageAllowlist(
                phash=image_hash.to_hex(phash), note="shadow", source="test", created_by="synthetic"
            )
        )
        await session.commit()
        observation = await image_hash.observe_shadow(
            session,
            attachments=[_Attachment("a.png"), _Attachment("note.txt", "text/plain")],
            media_dir=tmp_path,
            verdict=verdict,
            category=category,
            rule_ids=rule_ids,
        )
    assert observation is not None
    assert observation["mode"] == "shadow"
    assert observation["checked"] == 1, "只统计图片附件"
    assert observation["matched"] is True
    assert observation["would_allow"] is expected_would_allow
    assert observation["blocked_by"] == expected_blocked


async def test_observe_shadow_without_whitelist_hit(
    monkeypatch, clean_allowlist, tmp_path: Path
) -> None:
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    (tmp_path / "b.png").write_bytes(_image_bytes(2))
    async with SessionLocal() as session:
        observation = await image_hash.observe_shadow(
            session,
            attachments=[_Attachment("b.png")],
            media_dir=tmp_path,
            verdict="violation_high",
            category="ad",
            rule_ids=[],
        )
    assert observation is not None and observation["matched"] is False
    assert observation["checked"] == 1
