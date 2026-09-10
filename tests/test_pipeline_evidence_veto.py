"""Unknown or missing portions cannot be cleared by an unrelated readable image."""

import json
import uuid

import pytest
from app.core.contracts import Attachment, MessageSegment, Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIReviewService
from app.moderation.image_engine import MediaAnalysis
from app.runtime.pipeline import run_pipeline

from tests.test_ai_conditional_review import FakeVision


class NeutralImage:
    def analyze(self, _path):
        return MediaAnalysis("allow", 0.99, reason="test-local-readable")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown", "missing", "untranscribed_voice"])
async def test_incomplete_message_remains_manual_after_high_vision(
    tmp_path, monkeypatch, case
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    (media / "readable.png").write_bytes(b"test-only-image")
    (media / "voice.amr").write_bytes(b"test-only-audio")
    attachments = [Attachment(content_type="image/png", filename="readable.png")]
    segments = [MessageSegment(kind="image")]
    if case == "unknown":
        segments.append(MessageSegment(kind="unknown"))
    elif case == "missing":
        attachments.append(Attachment(content_type="image/png", filename="missing.png"))
    else:
        attachments.append(Attachment(content_type="voice", filename="voice.amr"))
    msg = StandardMessage(
        message_id=f"veto-{uuid.uuid4().hex}",
        provider="onebot",
        external_group_id="veto-test-group",
        external_user_id="test-member",
        sender=Sender(member_openid="test-member"),
        kind="mixed",
        text="今天一起读书",
        segments=segments,
        attachments=attachments,
    )

    class Source:
        provider = "onebot"

        def parse_group_message(self, _payload):
            return msg

    monkeypatch.setattr("app.runtime.pipeline.MEDIA_DIR", media)
    ai = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        vision_moderator=FakeVision("test-primary", "ad", 0.99),
    )
    async with SessionLocal() as session:
        record = await run_pipeline(
            {"id": msg.message_id},
            session,
            message_source=Source(),
            image_engine=NeutralImage(),
            ai_service=ai,
        )
    assert record is not None and record.verdict == "record_only"
    assert json.loads(record.detail_json)["recommended_actions"] == []
