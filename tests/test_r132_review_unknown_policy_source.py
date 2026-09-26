# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer round-9 probe pack (dbd80a5), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Synthetic-only review of the explicitly added assumed_defaults contract."""

import json
from datetime import UTC, datetime

import pytest

from app.moderation.ai import AIModerationResult, AIReviewService
from app.runtime import pipeline
from scripts.image_allowlist_seed import detail_blockers
from tests.test_r132_image_hash import _image_bytes, clean_allowlist
from tests.test_r132_review_offline_configured_review import FixedVision
from tests.test_r132_review_online_offline_pair_consistency import seed
from tests.test_r132_window_evidence import event, execute, ident


@pytest.mark.parametrize("source", ["service", "assumed_defaults"])
def test_assumed_defaults_are_not_evidence_of_actual_pair_policy(source):
    primary = AIModerationResult(
        source="vision",
        category="ad",
        confidence=0.70,
        needs_review=False,
        model_id="synthetic-primary",
        review_group="a",
        review_reason="confidence_gray_zone",
    )
    secondary = AIModerationResult(
        source="vision",
        category="ad",
        confidence=0.95,
        needs_review=False,
        model_id="synthetic-secondary",
        review_group="a",
        review_role="secondary",
        review_reason="confidence_gray_zone",
    )
    detail = {
        "ai_results": [primary.model_dump(), secondary.model_dump()],
        "review_policy": {
            "primary_direct_threshold": 0.90,
            "secondary_review_low": 0.60,
            "secondary_review_high": 0.90,
        },
        "review_policy_source": source,
    }
    blockers = detail_blockers(detail)
    if source == "service":
        assert blockers == []
    else:
        assert "unresolved_unknown" in blockers, (
            "An explicitly assumed policy was treated as verified evidence of secondary clearance",
            blockers,
        )


@pytest.mark.parametrize("opaque", [False, True])
async def test_hidden_nondefault_policy_cannot_be_replaced_with_known_defaults(
    monkeypatch, tmp_path, clean_allowlist, opaque
):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    data = _image_bytes(0)
    name = f"{ident()}.png"
    (tmp_path / name).write_bytes(data)
    await seed(data)
    group = str(ident())
    primary, secondary = FixedVision(0.70), FixedVision(0.95)
    actual = AIReviewService(
        enabled=True,
        enabled_groups={group},
        vision_moderator=primary,
        review_vision_moderator=secondary,
        secondary_review_high=0.99,
    )

    class OpaqueAdapter:
        """Same real reviewer, but no public threshold fields/snapshot accessor."""

        async def review_message(self, *args, **kwargs):
            return await actual.review_message(*args, **kwargs)

    service = OpaqueAdapter() if opaque else actual
    row = await execute(
        event(
            group, ident(), datetime.now(UTC), [{"type": "image", "data": {"file": name}}], (name,)
        ),
        service,
    )
    assert primary.calls == secondary.calls == 1
    assert row is not None and row.verdict == "record_only"
    detail = json.loads(row.detail_json)
    assert detail["image_hash"]["matched"] is True
    assert detail["image_hash"]["would_allow"] is False, detail
    blockers = detail_blockers(detail)
    assert blockers, detail
    if opaque:
        assert "unresolved_unknown" in blockers
    else:
        assert detail["review_policy"]["secondary_review_high"] == 0.99
        assert detail["review_policy_source"] == "service"
