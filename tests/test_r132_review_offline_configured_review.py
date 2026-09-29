# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-8 probe pack (b7d7e78), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Real AIReviewService/pipeline persistence, synthetic moderators, no network."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.moderation.ai import AIModerationResult, AIReviewService
from app.moderation.image_hash import dhash64
from app.runtime import pipeline
from scripts import image_allowlist_replay as replay_tool
from scripts import image_review_export as export_tool
from scripts.image_allowlist_seed import detail_blockers
from tests.test_r132_image_hash import _image_bytes, clean_allowlist
from tests.test_r132_review_online_offline_pair_consistency import seed
from tests.test_r132_window_evidence import event, execute, ident


class FixedVision:
    def __init__(self, confidence):
        self.confidence = confidence
        self.model_id = "synthetic-" + uuid4().hex
        self.prompt_version = "review-probe"
        self.calls = 0

    async def moderate_image(self, request):
        self.calls += 1
        return AIModerationResult(
            source="vision",
            model_id=self.model_id,
            category="ad",
            confidence=self.confidence,
            needs_review=False,
            evidence="synthetic",
        )


@pytest.mark.parametrize("tool", ["replay", "export"])
@pytest.mark.parametrize(
    "low,high,pconf,sconf,accepted",
    [
        (0.60, 0.95, 0.70, 0.92, False),
        (0.50, 0.90, 0.55, 0.95, True),
        (0.60, 0.90, 0.70, 0.95, True),
    ],
    ids=["stricter-high", "lower-low", "default-valid-control"],
)
async def test_offline_cannot_guess_thresholds_for_actual_persisted_pair(
    tmp_path, monkeypatch, clean_allowlist, tool, low, high, pconf, sconf, accepted
):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    data = _image_bytes(0)
    filename = f"{ident()}.png"
    (tmp_path / filename).write_bytes(data)
    await seed(data)
    primary, secondary = FixedVision(pconf), FixedVision(sconf)
    group = str(ident())
    service = AIReviewService(
        enabled=True,
        enabled_groups={group},
        vision_moderator=primary,
        review_vision_moderator=secondary,
        secondary_review_low=low,
        secondary_review_high=high,
    )
    row = await execute(
        event(
            group,
            ident(),
            datetime.now(UTC),
            [{"type": "image", "data": {"file": filename}}],
            (filename,),
        ),
        service,
    )
    assert row is not None
    assert primary.calls == secondary.calls == 1
    detail = json.loads(row.detail_json)
    assert row.verdict == ("violation_high" if accepted else "record_only"), row.reason
    assert detail["image_hash"]["matched"] is True
    assert detail["image_hash"]["would_allow"] is accepted, detail
    path = Path(os.environ["DATABASE_URL"].split(":///", 1)[1])
    kwargs = dict(
        db=path,
        media_dir=tmp_path,
        whitelist=[(dhash64(data), "db:enabled")],
        max_distance=2,
        limit=4000,
    )
    if tool == "replay":
        result = replay_tool.replay(**kwargs)
        changes = [x for x in result["would_change"] if x["message_id"] == row.message_id]
        # A missing historical policy may be UNKNOWN; it may not be guessed as clear.
        if not accepted:
            assert changes == [], (low, high, detail, result)
        else:
            evidence = next(
                (x for x in result["exceptions"] if x["message_id"] == row.message_id), None
            )
            assert changes or (evidence and "unresolved_unknown" in evidence["blocked_by"]), (
                low,
                high,
                detail,
                result,
            )
    else:
        result = export_tool.collect(**kwargs)
        changes = [x for x in result.values() if int(x["would_change"]) > 0]
        if not accepted:
            assert changes == [], (low, high, detail, result)
        else:
            blockers = detail_blockers(detail)
            assert changes or "unresolved_unknown" in blockers, (low, high, blockers, result)


@pytest.mark.parametrize("shape", ["partial_primary", "malformed_result", "review_trace_control"])
def test_missing_review_evidence_is_not_reported_as_known_clear(shape):
    if shape == "partial_primary":
        detail = {
            "ai_results": [
                {
                    "source": "vision",
                    "category": "ad",
                    "confidence": 0.7,
                    "model_id": "synthetic",
                    "needs_review": False,
                }
            ]
        }
    elif shape == "review_trace_control":
        detail = {
            "ai_results": [
                {
                    "source": "vision",
                    "category": "ad",
                    "confidence": 0.7,
                    "model_id": "synthetic",
                    "needs_review": False,
                    "review_reason": "confidence_gray_zone",
                }
            ]
        }
    else:
        detail = {
            "ai_results": [
                {
                    "source": "vision",
                    "category": "ad",
                    "confidence": "not-a-number",
                    "model_id": "synthetic",
                    "review_role": "primary",
                    "review_group": "a",
                    "needs_review": False,
                }
            ]
        }
    # No reconstructed valid proof of review is present in any shape.
    assert detail_blockers(detail), (shape, detail)
