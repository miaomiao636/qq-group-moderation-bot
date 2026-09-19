# ruff: noqa: E402, I001, F401, F811
# Reviewer round-6 probe pack (85b0c0b), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Mode controls on synthetic full-pipeline inputs; no production writes/calls."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.test_r132_window_evidence import (
    AIModerationResult,
    SyntheticModels,
    event,
    execute,
    ident,
)
from tests.test_r132_image_hash import _image_bytes, clean_allowlist
from app.db import SessionLocal
from app.models import ImageAllowlist
from app.moderation import image_hash
from app.runtime import pipeline


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "off"),
        ("off", "off"),
        ("nonsense", "off"),
        ("", "off"),
        (" ShAdOw ", "shadow"),
        ("enforce", "enforce"),
    ],
)
async def test_mode_never_changes_existing_decision(
    monkeypatch, tmp_path, clean_allowlist, raw, expected
):
    if raw is None:
        monkeypatch.delenv("IMAGE_HASH_MODE", raising=False)
    else:
        monkeypatch.setenv("IMAGE_HASH_MODE", raw)
    assert image_hash.mode() == expected
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    data = _image_bytes(0)
    (tmp_path / "sample.png").write_bytes(data)
    value = image_hash.dhash64(data)
    assert value is not None
    async with SessionLocal() as session:
        session.add(
            ImageAllowlist(phash=image_hash.to_hex(value), source="test", created_by="synthetic")
        )
        await session.commit()
    models = SyntheticModels(
        [
            AIModerationResult(
                source="vision",
                category="ad",
                confidence=0.99,
                needs_review=False,
                model_id="synthetic",
                review_group="a",
            )
        ]
    )
    payload = event(
        ident(),
        ident(),
        datetime.now(UTC),
        [{"type": "image", "data": {"file": "sample.png"}}],
        ("sample.png",),
    )
    row = await execute(payload, models)
    assert row is not None and row.verdict == "violation_high"
    detail = json.loads(row.detail_json)
    assert "recall" in detail["recommended_actions"]
    if expected == "off":
        assert "image_hash" not in detail
    else:
        assert detail["image_hash"]["mode"] == expected
        assert detail["image_hash"]["matched"] is True
    # Even the reserved string enforce is observation-only at this reviewed SHA.
