# CAMPUS-SCOPE-20260922: synthetic source fixtures adapted; see docs/2026-09-22-campus-source-scope.md.
# ruff: noqa: E402, I001, F401, F811, B905
# Reviewer round-4 probe pack (97d68d1), promoted verbatim into the repo test suite.
# This header changes no assertion and no logic.
"""Independent non-default direct/low/high routing and warm-cache verification."""

import uuid

import pytest

from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIReviewService
from app.moderation.decision import ModerationDecision


class Model:
    prompt_version = "synthetic-threshold-review"

    def __init__(self, confidence, *, primary, qr):
        self.model_id = ("primary-" if primary else "secondary-") + uuid.uuid4().hex
        self.confidence, self.primary, self.qr = confidence, primary, qr
        self.calls = 0

    async def moderate_image(self, request):
        self.calls += 1
        first = self.primary and request.media_bytes == b"first"
        return AIModerationResult(
            model_id=self.model_id,
            source="vision",
            category=None if first else "ad",
            confidence=1.0 if first else self.confidence,
            needs_review=False,
            has_miniprogram_code=first and self.qr,
            campus_wall_source="万能校园墙",
            evidence="校园墙白名单|文案:万能校园墙 合成活动",
        )


def setup(tmp_path, *, primary_score, secondary_score, direct, low, high, qr):
    paths = [tmp_path / "first.png", tmp_path / "second.png"]
    for path, data in zip(paths, (b"first", b"second")):
        path.write_bytes(data)
    group = "synthetic-" + uuid.uuid4().hex
    msg = StandardMessage(
        provider="onebot",
        external_group_id=group,
        message_id="probe-" + uuid.uuid4().hex,
        sender=Sender(member_openid="920055001"),
        kind="image",
    )
    local = ModerationDecision(
        provider="onebot",
        external_group_id=group,
        message_id=msg.message_id,
        verdict="allow",
        reason="synthetic neutral local",
    )
    primary = Model(primary_score, primary=True, qr=qr)
    secondary = Model(secondary_score, primary=False, qr=False)
    service = AIReviewService(
        enabled=True,
        enabled_groups={group},
        vision_moderator=primary,
        review_vision_moderator=secondary,
        primary_direct_threshold=direct,
        secondary_review_low=low,
        secondary_review_high=high,
    )
    return service, primary, secondary, msg, local, paths


@pytest.mark.parametrize("qr", [False, True])
@pytest.mark.parametrize(
    "primary_score,secondary_score,direct,low,high,secondary_calls,without_qr,with_qr",
    [
        (0.88, 0.95, 0.85, 0.60, 0.90, 0, "violation_high", "allow"),
        (0.75, 0.95, 0.90, 0.80, 0.90, 0, "allow", "allow"),
        (0.50, 0.94, 0.90, 0.40, 0.90, 1, "violation_high", "allow"),
        (0.50, 0.89, 0.90, 0.40, 0.90, 1, "record_only", "record_only"),
        (0.93, 0.91, 0.97, 0.70, 0.92, 1, "record_only", "record_only"),
        (0.70, 0.85, 0.90, 0.60, 0.80, 1, "violation_high", "allow"),
        (0.85, 0.95, 0.97, 0.80, 0.95, 1, "violation_high", "allow"),
        (0.97, 0.95, 0.97, 0.80, 0.95, 0, "violation_high", "allow"),
    ],
    ids=[
        "loose-direct",
        "strict-low",
        "loose-low-valid",
        "loose-low-invalid",
        "strict-direct-high",
        "loose-high",
        "three-configured-valid",
        "at-direct-boundary",
    ],
)
async def test_nondefault_threshold_matrix_cold_and_warm(
    tmp_path,
    qr,
    primary_score,
    secondary_score,
    direct,
    low,
    high,
    secondary_calls,
    without_qr,
    with_qr,
):
    service, primary, secondary, msg, local, paths = setup(
        tmp_path,
        primary_score=primary_score,
        secondary_score=secondary_score,
        direct=direct,
        low=low,
        high=high,
        qr=qr,
    )
    async with SessionLocal() as session:
        cold, cold_results = await service.review_message(session, msg, local, media_paths=paths)
        warm, warm_results = await service.review_message(session, msg, local, media_paths=paths)
    expected = with_qr if qr else without_qr
    assert cold.verdict == warm.verdict == expected
    assert primary.calls == 2
    assert secondary.calls == secondary_calls
    assert len([r for r in cold_results if r.review_role == "secondary"]) == secondary_calls
    assert all(r.cache_hit for r in warm_results)
    if expected != "violation_high":
        assert cold.recommended_actions == warm.recommended_actions == []


@pytest.mark.parametrize(
    "field,new_value,primary_score,secondary_score,high,before_secondary,after_secondary",
    [
        ("primary_direct_threshold", 0.97, 0.93, 0.91, 0.92, 0, 1),
        ("secondary_review_low", 0.40, 0.50, 0.89, 0.90, 0, 1),
        ("secondary_review_high", 0.95, 0.70, 0.92, 0.90, 1, 1),
    ],
    ids=["change-direct", "change-low", "change-high"],
)
async def test_changing_threshold_invalidates_qr_allow_cache(
    tmp_path,
    field,
    new_value,
    primary_score,
    secondary_score,
    high,
    before_secondary,
    after_secondary,
):
    service, primary, secondary, msg, local, paths = setup(
        tmp_path,
        primary_score=primary_score,
        secondary_score=secondary_score,
        direct=0.90,
        low=0.60,
        high=high,
        qr=True,
    )
    async with SessionLocal() as session:
        before, _ = await service.review_message(session, msg, local, media_paths=paths)
        assert before.verdict == "allow"
        assert (primary.calls, secondary.calls) == (2, before_secondary)
        setattr(service, field, new_value)
        after, results = await service.review_message(session, msg, local, media_paths=paths)
        warm, warm_results = await service.review_message(session, msg, local, media_paths=paths)
    assert after.verdict == warm.verdict == "record_only"
    assert after.recommended_actions == warm.recommended_actions == []
    assert (primary.calls, secondary.calls) == (4, before_secondary + after_secondary)
    assert all(not r.cache_hit for r in results)
    assert all(r.cache_hit for r in warm_results)
