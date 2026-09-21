# ruff: noqa: E402, I001, F401, F811
# Reviewer round-6 probe pack (85b0c0b), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Expected-contract probes for extra commit 60796a5; synthetic inputs only."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Reuse repository isolation guards before application imports.
from tests.test_r132_window_evidence import (
    AIModerationResult,
    SyntheticModels,
    event,
    execute,
    ident,
)
from tests.test_r132_image_hash import _image_bytes, _Attachment, clean_allowlist
from app.db import SessionLocal
from app.models import ImageAllowlist
from app.moderation import image_hash
from app.runtime import pipeline


async def seed_hash(data, *, xor=0):
    value = image_hash.dhash64(data)
    assert value is not None
    async with SessionLocal() as session:
        session.add(
            ImageAllowlist(
                phash=image_hash.to_hex(value ^ xor), source="test", created_by="synthetic"
            )
        )
        await session.commit()


def image_event(files):
    return event(
        ident(),
        ident(),
        datetime.now(UTC),
        [{"type": "image", "data": {"file": filename}} for filename in files],
        files,
    )


def model_results(*, severe=False):
    values = [
        AIModerationResult(
            source="vision",
            category="ad",
            confidence=0.99,
            needs_review=False,
            model_id="synthetic",
            review_group="a",
        )
    ]
    if severe:
        values.append(
            AIModerationResult(
                source="vision",
                category="porn",
                confidence=0.95,
                needs_review=False,
                model_id="synthetic",
                review_group="b",
            )
        )
    return SyntheticModels(values)


@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_observer_read_failure_must_not_abort_existing_pipeline(
    monkeypatch, tmp_path, clean_allowlist, mode
):
    monkeypatch.setenv("IMAGE_HASH_MODE", mode)
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    picture = tmp_path / "a.png"
    picture.write_bytes(_image_bytes(0))
    original_read = Path.read_bytes
    calls = []

    def unavailable(path):
        if path == picture:
            calls.append(True)
            raise PermissionError("synthetic media permission failure")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    row = await execute(image_event(("a.png",)), model_results())
    assert bool(calls) == (mode == "shadow")
    assert row is not None, "shadow-only observer aborted the main pipeline"
    assert row.verdict == "violation_high"
    assert "recall" in json.loads(row.detail_json)["recommended_actions"]


async def test_shadow_would_allow_must_preserve_other_image_severe_evidence(
    monkeypatch, tmp_path, clean_allowlist
):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    data = _image_bytes(0)
    (tmp_path / "a.png").write_bytes(data)
    (tmp_path / "b.png").write_bytes(_image_bytes(2))
    await seed_hash(data)
    row = await execute(image_event(("a.png", "b.png")), model_results(severe=True))
    assert row is not None and row.verdict == "violation_high"
    details = json.loads(row.detail_json)
    assert row.category == "ad" and any(h["category"] == "porn" for h in details["rule_hits"])
    assert details["image_hash"]["matched"] is True
    assert details["image_hash"]["would_allow"] is False, details["image_hash"]


async def test_shadow_would_allow_must_not_clear_missing_attachment_veto(
    monkeypatch, tmp_path, clean_allowlist
):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    data = _image_bytes(0)
    (tmp_path / "a.png").write_bytes(data)
    await seed_hash(data)
    row = await execute(image_event(("a.png", "missing.png")), model_results())
    assert row is not None and row.verdict == "record_only"
    details = json.loads(row.detail_json)
    assert "媒体缺失/下载失败" in details["evidence_vetoes"]
    assert details["recommended_actions"] == []
    assert details["image_hash"]["would_allow"] is False, details["image_hash"]


@pytest.mark.parametrize("explicit_two", [False, True])
async def test_shadow_observation_must_use_reviewed_two_bit_threshold(
    monkeypatch, tmp_path, clean_allowlist, explicit_two
):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    data = _image_bytes(0)
    (tmp_path / "a.png").write_bytes(data)
    await seed_hash(data, xor=7)  # exactly 3 bits from the stored seed
    kwargs = {"max_distance": 2} if explicit_two else {}
    async with SessionLocal() as session:
        observation = await image_hash.observe_shadow(
            session,
            attachments=[_Attachment("a.png")],
            media_dir=tmp_path,
            verdict="violation_high",
            category="ad",
            rule_ids=[],
            **kwargs,
        )
    assert observation is not None
    assert observation["matched"] is False, observation
