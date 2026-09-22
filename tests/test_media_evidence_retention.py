"""Media evidence retention with the real service short-circuit and isolated fakes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.qq_official.parser import QQOfficialMessageSource
from app.core.contracts import Attachment
from app.db import SessionLocal
from app.moderation.ai import AIReviewService
from app.moderation.allowlist import add_member
from app.moderation.image_engine import MediaAnalysis
from app.moderation.media_engine import evaluate_file, evaluate_voice
from app.moderation.rules import TextRuleEngine, evaluate_text
from app.runtime import pipeline

from tests.test_campus_share_template import result
from tests.test_r132_window_evidence import (
    LocalImageAllow,
    event,
    execute,
    ident,
    source_image,
)


class FixedCampusVision:
    model_id = "media-evidence-synthetic"
    prompt_version = "media-evidence-test"

    def __init__(self):
        self.calls = 0

    async def moderate_image(self, request):
        self.calls += 1
        return result().model_copy(update={"model_id": self.model_id})


def service():
    fake = FixedCampusVision()
    return AIReviewService(enabled=True, enabled_groups={"*"}, vision_moderator=fake), fake


def evidence_categories(row, detail):
    return {row.category} | {hit["category"] for hit in detail["rule_hits"]}


@pytest.mark.parametrize("term,category", [("色情", "porn"), ("枪支", "violence")])
@pytest.mark.parametrize("kind", ["file", "voice"])
def test_media_evaluators_preserve_real_rule_hits(tmp_path, term, category, kind):
    text = term + " 加我微信 abc12345"
    expected_hits, confidence, expected_category = evaluate_text(text)
    assert expected_category == category and confidence >= 0.90
    if kind == "file":
        path = tmp_path / "synthetic.txt"
        path.write_text(text, encoding="utf-8")
        decision = evaluate_file("msg", "group", "member", path, TextRuleEngine())
    else:
        decision = evaluate_voice(
            "msg", "group", "member", Attachment(asr_refer_text=text), TextRuleEngine()
        )
    assert decision.verdict == "violation_high"
    assert decision.category == category
    assert decision.rule_hits == expected_hits
    assert decision.recommended_actions == ["recall", "mute", "warn"]


@pytest.mark.asyncio
@pytest.mark.parametrize("term,category", [("色情", "porn"), ("枪支", "violence")])
@pytest.mark.parametrize("add_image", [False, True])
async def test_actual_service_preserves_file_evidence(
    monkeypatch, tmp_path, term, category, add_image
):
    path = tmp_path / "synthetic.txt"
    path.write_text(term + " 加我微信 abc12345", encoding="utf-8")
    (tmp_path / "card.png").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    segments = [{"type": "file", "data": {"file": path.name}}]
    files = [path.name]
    if add_image:
        segments.append({"type": "image", "data": {"file": "card.png"}})
        files.append("card.png")
    payload = event(ident(), ident(), datetime.now(UTC), segments, files)
    ai, fake = service()
    row = await execute(payload, ai)
    assert row is not None
    detail = json.loads(row.detail_json)
    assert row.verdict == "violation_high"
    assert detail["recommended_actions"] == ["recall", "mute", "warn"]
    assert fake.calls == 0 and detail["ai_results"] == []
    assert category in evidence_categories(row, detail)
    assert {"R001", "R003"} <= {h["rule_id"] for h in detail["rule_hits"]}
    assert "campus_source_policy" not in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("term,category", [("色情", "porn"), ("枪支", "violence")])
async def test_official_voice_retains_severe_evidence(monkeypatch, tmp_path, term, category):
    (tmp_path / "voice.amr").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    payload = {
        "id": "media-voice-" + uuid.uuid4().hex,
        "group_openid": "media-group-" + uuid.uuid4().hex,
        "author": {"member_openid": "media-member"},
        "attachments": [
            {
                "content_type": "voice",
                "filename": "voice.amr",
                "asr_refer_text": term + " 加我微信 abc12345",
            }
        ],
    }
    ai, fake = service()
    async with SessionLocal() as session:
        row = await pipeline.run_pipeline(
            payload,
            session,
            ai_service=ai,
            message_source=QQOfficialMessageSource(),
            image_engine=LocalImageAllow(),
        )
    assert row is not None
    detail = json.loads(row.detail_json)
    assert row.verdict == "violation_high"
    assert fake.calls == 0 and detail["ai_results"] == []
    assert detail["recommended_actions"] == ["recall", "mute", "warn"]
    assert category in evidence_categories(row, detail)
    assert {"R001", "R003"} <= {h["rule_id"] for h in detail["rule_hits"]}
    assert "abc12345" not in json.dumps(detail["rule_hits"])


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_each_media_category_survives_attachment_order(monkeypatch, tmp_path, reverse):
    files = ["ad.txt", "severe.txt"]
    (tmp_path / files[0]).write_text("招募兼职刷单，日结，加我微信 abc12345", encoding="utf-8")
    (tmp_path / files[1]).write_text("色情 加我微信 abc12345", encoding="utf-8")
    if reverse:
        files.reverse()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    payload = event(
        ident(),
        ident(),
        datetime.now(UTC),
        [{"type": "file", "data": {"file": name}} for name in files],
        files,
    )
    ai, fake = service()
    row = await execute(payload, ai)
    assert row is not None and row.verdict == "violation_high"
    detail = json.loads(row.detail_json)
    assert fake.calls == 0
    assert {"ad", "porn"} <= evidence_categories(row, detail)


@pytest.mark.asyncio
async def test_severe_media_does_not_change_member_allowlist(monkeypatch, tmp_path):
    user = ident()
    async with SessionLocal() as session:
        await add_member(session, str(user), operator="synthetic-test")
    (tmp_path / "severe.txt").write_text("色情 加我微信 abc12345", encoding="utf-8")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    payload = event(
        ident(),
        user,
        datetime.now(UTC),
        [{"type": "file", "data": {"file": "severe.txt"}}],
        ["severe.txt"],
    )
    ai, _fake = service()
    row = await execute(payload, ai)
    assert row is not None and row.verdict == "allow" and row.category == ""
    detail = json.loads(row.detail_json)
    assert detail["recommended_actions"] == []
    assert "porn" in evidence_categories(row, detail)
    assert "campus_source_policy" not in detail


@pytest.mark.asyncio
async def test_missing_voice_stays_manual(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    payload = event(
        ident(),
        ident(),
        datetime.now(UTC),
        [{"type": "record", "data": {"file": "missing.amr"}}],
        ["missing.amr"],
    )
    ai, fake = service()
    row = await execute(payload, ai)
    assert row is not None and row.verdict == "record_only"
    detail = json.loads(row.detail_json)
    assert detail["recommended_actions"] == [] and detail["evidence_vetoes"]
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_campus_image_still_uses_real_provider_flow(monkeypatch, tmp_path):
    (tmp_path / "card.png").write_bytes(uuid.uuid4().bytes)
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    payload = event(
        ident(),
        ident(),
        datetime.now(UTC),
        [{"type": "image", "data": {"file": "card.png"}}],
        ["card.png"],
    )
    ai, fake = service()
    row = await execute(payload, ai)
    assert row is not None and row.verdict == "allow"
    detail = json.loads(row.detail_json)
    assert fake.calls == 1 and detail["recommended_actions"] == []
    assert "campus_source_policy" in detail


@pytest.mark.asyncio
async def test_adding_audit_category_does_not_newly_grant_window(monkeypatch, tmp_path):
    group, user = ident(), ident()
    when = datetime.now(UTC).replace(microsecond=0)
    await source_image(group, user, when - timedelta(seconds=10))
    (tmp_path / "black.png").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)

    class LocalImageHigh:
        def analyze(self, _path):
            return MediaAnalysis("violation_high", 0.95, reason="synthetic confirmed hash")

    payload = event(
        group, user, when, [{"type": "image", "data": {"file": "black.png"}}], ["black.png"]
    )
    ai, fake = service()
    async with SessionLocal() as session:
        from app.adapters.onebot.parser import OneBotMessageSource

        row = await pipeline.run_pipeline(
            payload,
            session,
            ai_service=ai,
            message_source=OneBotMessageSource(),
            image_engine=LocalImageHigh(),
            dedup_key=f"onebot:10000001:{payload['message_id']}",
        )
    assert row is not None and row.verdict == "violation_high"
    detail = json.loads(row.detail_json)
    assert fake.calls == 0
    assert detail["recommended_actions"] == ["recall", "mute", "warn"]
    assert "campus_source_policy" not in detail
