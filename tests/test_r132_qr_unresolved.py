# CAMPUS-SCOPE-20260922: synthetic source fixtures adapted; see docs/2026-09-22-campus-source-scope.md.
# ruff: noqa: E402, I001, F401
# Reviewer probe pack (PR #45 / r132), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.

import json
import uuid

import pytest

from app.core.contracts import Attachment, Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIProviderError, AIReviewService
from app.moderation.image_engine import MediaAnalysis
from app.runtime import pipeline


class NeutralImage:
    def analyze(self, path):
        return MediaAnalysis("record_only", 0.1, reason="synthetic unresolved local image")


class FakeVision:
    def __init__(self, qr, second):
        self.qr = qr
        self.second = second
        self.model_id = "r132-fake-" + uuid.uuid4().hex
        self.prompt_version = "synthetic-r132"

    async def moderate_image(self, request):
        if request.media_bytes == b"synthetic-second-image" and self.second == "timeout":
            raise AIProviderError("synthetic_timeout")
        second = request.media_bytes == b"synthetic-second-image"
        return AIModerationResult(
            model_id=self.model_id,
            source="vision",
            confidence=0.99,
            category=None if not second or self.second != "porn" else "porn",
            needs_review=second and self.second == "unresolved",
            has_miniprogram_code=self.qr and not second,
            campus_wall_source="万能校园墙",
            evidence="校园墙白名单|文案:万能校园墙 合成活动",
        )


@pytest.mark.parametrize("qr", [False, True])
@pytest.mark.parametrize("second", ["normal", "unresolved", "timeout", "porn"])
async def test_a_qr_image_must_not_clear_another_image_unresolved_review(
    tmp_path, monkeypatch, qr, second
):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    (tmp_path / "first.png").write_bytes(b"synthetic-first-image")
    (tmp_path / "second.png").write_bytes(b"synthetic-second-image")
    msg = StandardMessage(
        provider="onebot",
        message_id="r132-multi-" + uuid.uuid4().hex,
        external_group_id="r132-group-" + uuid.uuid4().hex,
        sender=Sender(member_openid="10000000002", role="member"),
        kind="image",
        text="",
        attachments=[
            Attachment(content_type="image/png", filename=name)
            for name in ("first.png", "second.png")
        ],
    )

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    ai = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        vision_moderator=FakeVision(qr, second),
    )
    async with SessionLocal() as session:
        record = await pipeline.run_pipeline(
            {"message_id": msg.message_id},
            session,
            message_source=Source(),
            image_engine=NeutralImage(),
            ai_service=ai,
        )
    assert record is not None
    detail = json.loads(record.detail_json)
    if second == "porn":
        assert record.verdict == "violation_high"
        assert "recall" in detail["recommended_actions"]
        return
    assert detail["recommended_actions"] == []
    if second in ("unresolved", "timeout"):
        assert record.verdict == "record_only", detail
    elif qr:
        assert record.verdict == "allow"
