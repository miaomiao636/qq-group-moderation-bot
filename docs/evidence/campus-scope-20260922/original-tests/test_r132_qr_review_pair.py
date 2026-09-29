# ruff: noqa: E402, I001, F401
# Reviewer round-2 probe pack (a354d17), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.
"""Independent F02 adjacent probes; no live data, no real model or action calls."""

import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest

assert os.environ.get("APP_ENV") == "test"
assert os.environ.get("ACTION_MODE") == "SHADOW"
for flag in (
    "AI_ENABLED",
    "ONEBOT_ACTIONS_ENABLED",
    "NOTIFICATIONS_ENABLED",
    "NOTIFICATION_QQ_ENABLED",
    "NOTIFICATION_EMAIL_ENABLED",
    "NOTIFICATION_HEARTBEAT_ENABLED",
):
    assert os.environ.get(flag) == "false", flag
url = os.environ.get("DATABASE_URL", "")
assert url.startswith("sqlite+aiosqlite:///")
db = Path(url.split(":///", 1)[1]).resolve()
assert db.parent.name.startswith("qqbot-test-")
assert db.is_relative_to(Path(tempfile.gettempdir()).resolve())

from app.core.contracts import Attachment, Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIReviewService
from app.moderation.image_engine import MediaAnalysis
from app.runtime import pipeline


class LocalNeutral:
    def analyze(self, path):
        return MediaAnalysis("record_only", 0.1, reason="synthetic undecided media")


class FakePrimary:
    def __init__(self, qr, scenario):
        self.model_id = "primary-" + uuid.uuid4().hex
        self.prompt_version = "synthetic"
        self.qr = qr
        self.scenario = scenario

    async def moderate_image(self, request):
        first = request.media_bytes == b"first"
        return AIModerationResult(
            model_id=self.model_id,
            source="vision",
            category=None if first or self.scenario == "normal" else "ad",
            confidence=1.0 if first or self.scenario == "normal" else 0.7,
            needs_review=False,
            has_miniprogram_code=first and self.qr,
            evidence="synthetic primary",
        )


class FakeSecondary:
    def __init__(self, scenario):
        self.model_id = "secondary-" + uuid.uuid4().hex
        self.prompt_version = "synthetic"
        self.scenario = scenario

    async def moderate_image(self, request):
        return AIModerationResult(
            model_id=self.model_id,
            source="vision",
            category=None if self.scenario == "disagree" else "ad",
            confidence=1.0 if self.scenario == "disagree" else 0.5,
            needs_review=False,
            has_miniprogram_code=False,
            evidence="synthetic secondary",
        )


@pytest.mark.parametrize("qr", [False, True])
@pytest.mark.parametrize("scenario", ["disagree", "weak_confirmation", "normal"])
async def test_qr_cannot_resolve_another_images_invalid_review_pair(
    tmp_path, monkeypatch, qr, scenario
):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    (tmp_path / "first.png").write_bytes(b"first")
    (tmp_path / "second.png").write_bytes(b"second")
    msg = StandardMessage(
        provider="onebot",
        message_id="probe-" + uuid.uuid4().hex,
        external_group_id="synthetic-" + uuid.uuid4().hex,
        sender=Sender(member_openid="910099001", role="member"),
        kind="image",
        attachments=[
            Attachment(content_type="image/png", filename=f"{part}.png")
            for part in ("first", "second")
        ],
    )

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    service = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        vision_moderator=FakePrimary(qr, scenario),
        review_vision_moderator=FakeSecondary(scenario),
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
    assert detail["recommended_actions"] == []
    if scenario != "normal":
        pairs = [r for r in detail["ai_results"] if r["review_role"] == "secondary"]
        assert len(pairs) == 1
        assert all(not r["needs_review"] and not r["degraded_reason"] for r in detail["ai_results"])
        assert record.verdict == "record_only", (
            record.verdict,
            record.reason,
            detail["ai_results"],
        )
    elif qr:
        assert record.verdict == "allow"
