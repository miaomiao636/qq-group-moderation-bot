# ruff: noqa: I001
# Reviewer round-3 probe pack (7ec5553), promoted verbatim into the repo test suite.
# This header changes no assertion and no logic.
"""Synthetic pipeline tests: the QR gate must use the service's actual thresholds."""

import json
import uuid

import pytest

from app.core.contracts import Attachment, Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIReviewService
from app.moderation.image_engine import MediaAnalysis
from app.runtime import pipeline


class LocalNeutral:
    def analyze(self, path):
        return MediaAnalysis("record_only", 0.1, reason="synthetic undecided media")


class Primary:
    def __init__(self, qr):
        self.model_id = "synthetic-primary-" + uuid.uuid4().hex
        self.prompt_version = "synthetic"
        self.qr = qr

    async def moderate_image(self, request):
        first = request.media_bytes == b"first"
        return AIModerationResult(
            model_id=self.model_id,
            source="vision",
            category=None if first else "ad",
            confidence=1.0 if first else 0.7,
            needs_review=False,
            has_miniprogram_code=first and self.qr,
            evidence="synthetic only",
        )


class Secondary:
    def __init__(self, confidence):
        self.model_id = "synthetic-secondary-" + uuid.uuid4().hex
        self.prompt_version = "synthetic"
        self.confidence = confidence

    async def moderate_image(self, request):
        return AIModerationResult(
            model_id=self.model_id,
            source="vision",
            category="ad",
            confidence=self.confidence,
            needs_review=False,
            evidence="synthetic only",
        )


@pytest.mark.parametrize("qr", [False, True])
@pytest.mark.parametrize(
    "high,secondary_confidence,accepted",
    [(0.95, 0.92, False), (0.90, 0.89, False), (0.90, 0.92, True)],
    ids=["configured-strict", "default-invalid-control", "default-valid-control"],
)
async def test_qr_gate_respects_configured_secondary_threshold(
    tmp_path, monkeypatch, qr, high, secondary_confidence, accepted
):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    for name in ("first", "second"):
        (tmp_path / f"{name}.png").write_bytes(name.encode())
    msg = StandardMessage(
        provider="onebot",
        message_id="probe-" + uuid.uuid4().hex,
        external_group_id="synthetic-" + uuid.uuid4().hex,
        sender=Sender(member_openid="910099002", role="member"),
        kind="image",
        attachments=[
            Attachment(content_type="image/png", filename=f"{name}.png")
            for name in ("first", "second")
        ],
    )

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    service = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        vision_moderator=Primary(qr),
        review_vision_moderator=Secondary(secondary_confidence),
        secondary_review_high=high,
    )
    async with SessionLocal() as session:
        record = await pipeline.run_pipeline(
            {"message_id": msg.message_id},
            session,
            message_source=Source(),
            ai_service=service,
            image_engine=LocalNeutral(),
        )
    assert record is not None
    detail = json.loads(record.detail_json)
    pairs = [r for r in detail["ai_results"] if r["review_role"] == "secondary"]
    assert len(pairs) == 1
    assert pairs[0]["confidence"] == secondary_confidence
    if not accepted:
        assert record.verdict == "record_only", (record.verdict, record.reason, high, pairs)
        assert detail["recommended_actions"] == []
    elif qr:
        assert record.verdict == "allow"
