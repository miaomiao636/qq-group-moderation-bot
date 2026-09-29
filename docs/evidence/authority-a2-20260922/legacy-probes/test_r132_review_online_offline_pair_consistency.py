# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-7 probe pack (2ff2a8a), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Round6 probes: real persisted decisions, synthetic models and images only."""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

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
from scripts import image_allowlist_replay as replay_tool
from scripts import image_review_export as export_tool


async def seed(data):
    phash = image_hash.dhash64(data)
    assert phash is not None
    async with SessionLocal() as session:
        session.add(
            ImageAllowlist(phash=image_hash.to_hex(phash), source="test", created_by="synthetic")
        )
        await session.commit()
    return phash


@pytest.mark.parametrize("variant", ["disagree", "low_secondary", "same_model", "valid"])
@pytest.mark.parametrize("tool", ["replay", "export"])
async def test_offline_full_evidence_must_match_online_pair_result(
    monkeypatch, tmp_path, clean_allowlist, variant, tool
):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    data = _image_bytes(0)
    filename = f"{ident()}.png"
    (tmp_path / filename).write_bytes(data)
    phash = await seed(data)
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
    payload = event(
        ident(),
        ident(),
        datetime.now(UTC),
        [{"type": "image", "data": {"file": filename}}],
        (filename,),
    )
    row = await execute(payload, SyntheticModels([primary, secondary]))
    assert row is not None
    detail = json.loads(row.detail_json)
    valid = variant == "valid"
    assert row.verdict == ("violation_high" if valid else "record_only")
    assert detail["image_hash"]["would_allow"] is valid, detail
    # conftest + imported guard proved this URL points to its private temp DB.
    database = Path(os.environ["DATABASE_URL"].split(":///", 1)[1])
    kwargs = dict(
        db=database,
        media_dir=tmp_path,
        whitelist=[(phash, "db:enabled")],
        max_distance=2,
        limit=4000,
    )
    if tool == "replay":
        report = replay_tool.replay(**kwargs)
        changed = any(item["message_id"] == row.message_id for item in report["would_change"])
    else:
        report = export_tool.collect(**kwargs)
        changed = any(int(item["would_change"]) > 0 for item in report.values())
    assert changed is valid, (variant, tool, row.reason, report)


async def test_animation_observation_is_only_first_frame(monkeypatch, tmp_path, clean_allowlist):
    """Passing diagnostic: a scope label exists but does not mean full-animation equivalence."""
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    with Image.open(io.BytesIO(_image_bytes(0))) as first:
        output = io.BytesIO()
        first.convert("RGB").save(
            output,
            format="GIF",
            save_all=True,
            append_images=[Image.new("RGB", first.size, "black")],
            duration=[100, 900],
            loop=0,
        )
    data = output.getvalue()
    (tmp_path / "sample.gif").write_bytes(data)
    await seed(data)
    async with SessionLocal() as session:
        result = await image_hash.observe_shadow(
            session,
            attachments=[_Attachment("sample.gif", "image/gif")],
            media_dir=tmp_path,
            verdict="record_only",
            category="",
            rule_ids=[],
        )
    assert result is not None and result["matched"] is True
    assert result["frame_scope"] == "first_frame"
    assert result["would_allow"] is True  # Current counterfactual flag, NOT reviewer endorsement.
