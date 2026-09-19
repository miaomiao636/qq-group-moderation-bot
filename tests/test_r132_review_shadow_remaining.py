# ruff: noqa: E402, I001, F401, F811
# Reviewer round-6 probe pack (85b0c0b), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Synthetic review probes for head 85b0c0b. No real QQ or model calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def seed(data):
    value = image_hash.dhash64(data)
    assert value is not None
    async with SessionLocal() as session:
        session.add(
            ImageAllowlist(phash=image_hash.to_hex(value), source="synthetic", created_by="review")
        )
        await session.commit()


def payload():
    return event(
        ident(),
        ident(),
        datetime.now(UTC),
        [
            {"type": "image", "data": {"file": "a.png"}},
        ],
        ("a.png",),
    )


@pytest.mark.parametrize("mode", ["off", "invalid-mode", "shadow"])
@pytest.mark.parametrize("fail_stat", [False, True])
async def test_pre_observer_stat_failure_must_not_abort_pipeline(
    monkeypatch, tmp_path, mode, fail_stat
):
    monkeypatch.setenv("IMAGE_HASH_MODE", mode)
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    picture = tmp_path / "a.png"
    picture.write_bytes(_image_bytes(0))
    calls = []
    analysis_complete = False
    original = Path.is_file

    def permission_failure(path):
        if path == picture and analysis_complete:
            calls.append(str(path))
            if fail_stat:
                raise PermissionError("synthetic stat permission error")
        return original(path)

    monkeypatch.setattr(Path, "is_file", permission_failure)

    class PostAnalysisModels(SyntheticModels):
        async def review_message(self, *args, **kwargs):
            nonlocal analysis_complete
            result = await super().review_message(*args, **kwargs)
            analysis_complete = True
            return result

    row = await execute(
        payload(),
        PostAnalysisModels(
            [
                AIModerationResult(
                    source="vision",
                    category="ad",
                    confidence=0.99,
                    needs_review=False,
                    model_id="synthetic",
                    review_group="a",
                ),
            ]
        ),
    )
    assert row is not None, (
        f"mode={mode}: new observer preparation aborted the pipeline; calls={calls}"
    )
    assert row.verdict == "violation_high"
    if mode != "shadow":
        if fail_stat:
            assert calls == [], "off must skip observer-only file I/O"
        assert "image_hash" not in json.loads(row.detail_json)


@pytest.mark.parametrize("variant", ["disagree", "low_secondary", "same_model", "valid"])
async def test_unresolved_secondary_pair_must_block_shadow_would_allow(
    monkeypatch, tmp_path, clean_allowlist, variant
):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    data = _image_bytes(0)
    (tmp_path / "a.png").write_bytes(data)
    await seed(data)
    primary = AIModerationResult(
        source="vision",
        category="ad",
        confidence=0.70,
        needs_review=False,
        model_id="primary",
        review_group="a",
        review_reason="confidence_gray_zone",
    )
    secondary = AIModerationResult(
        source="vision",
        category="fraud" if variant == "disagree" else "ad",
        confidence=0.70 if variant == "low_secondary" else 0.99,
        needs_review=False,
        model_id="primary" if variant == "same_model" else "secondary",
        review_role="secondary",
        review_group="a",
        review_reason="confidence_gray_zone",
    )
    row = await execute(payload(), SyntheticModels([primary, secondary]))
    assert row is not None
    assert row.verdict == ("violation_high" if variant == "valid" else "record_only"), row
    detail = json.loads(row.detail_json)
    if variant != "valid":
        assert detail["recommended_actions"] == []
    assert detail["image_hash"]["matched"] is True
    assert detail["image_hash"]["would_allow"] is (variant == "valid"), (
        row.reason,
        detail["image_hash"],
    )


async def test_missing_allowlist_table_is_unavailable_not_clean_miss(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    (tmp_path / "a.png").write_bytes(_image_bytes(0))
    # Deliberately empty, private in-memory database: do not touch migrated shared tables.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with AsyncSession(engine) as session:
            observation = await image_hash.observe_shadow(
                session,
                attachments=[_Attachment("a.png")],
                media_dir=tmp_path,
                verdict="violation_high",
                category="ad",
                rule_ids=[],
            )
        assert observation is not None and observation["matched"] is False
        assert observation.get("unavailable") == "db_failed", observation
    finally:
        await engine.dispose()
