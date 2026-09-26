"""Real OneBot text/image normalization must preserve the approved window policy."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from app.adapters.onebot.parser import OneBotMessageSource
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult
from app.runtime import pipeline
from app.runtime.models import ShadowDecision
from sqlalchemy import select

from tests.test_campus_share_template import result
from tests.test_r132_source_message_order import reload_persisted
from tests.test_r132_structure_probe import json_segment
from tests.test_r132_window_evidence import SyntheticModels, event, execute, ident


def image_segments(name="source.png"):
    return [
        {"type": "text", "data": {"text": "合成配图说明"}},
        {"type": "image", "data": {"file": name}},
    ]


def ad_model(kind="text", category="ad"):
    return AIModerationResult(
        source="text" if kind == "text" else "vision",
        category=category,
        confidence=0.99,
        needs_review=False,
        model_id="synthetic-followup",
    )


async def mixed_source(monkeypatch, tmp_path, *, extra=(), missing=False):
    group, user = ident(), ident()
    when = datetime.now(UTC).replace(microsecond=0)
    if not missing:
        (tmp_path / "source.png").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    payload = event(group, user, when, image_segments() + list(extra), ("source.png",))
    parsed = OneBotMessageSource().parse_group_message(payload)
    if not extra:
        assert parsed.kind == "mixed"
    row = await execute(payload, SyntheticModels([result()]))
    return await reload_persisted(row), group, user, when


@pytest.mark.parametrize("kind", ["text", "image", "mixed"])
@pytest.mark.parametrize("seconds", [0, 30, 120, 121])
async def test_text_image_source_and_followup_window(monkeypatch, tmp_path, kind, seconds):
    row, group, user, when = await mixed_source(monkeypatch, tmp_path)
    assert row.kind == "mixed" and row.verdict == "allow"
    assert json.loads(row.detail_json).get("campus_source_policy")
    (tmp_path / "next.png").touch()
    segments = (
        [{"type": "text", "data": {"text": "合成后续推广"}}]
        if kind == "text"
        else image_segments("next.png")[1:]
        if kind == "image"
        else image_segments("next.png")
    )
    payload = event(group, user, when + timedelta(seconds=seconds), segments, ("next.png",))
    current = await reload_persisted(await execute(payload, SyntheticModels([ad_model(kind)])))
    exempt = 0 < seconds <= 120
    assert current.verdict == ("record_only" if exempt else "violation_high")
    actions = json.loads(current.detail_json)["recommended_actions"]
    assert actions == [] if exempt else "recall" in actions


@pytest.mark.parametrize("identity", ["user", "group", "account"])
async def test_mixed_source_never_crosses_identity(monkeypatch, tmp_path, identity):
    _, group, user, when = await mixed_source(monkeypatch, tmp_path)
    payload = event(
        ident() if identity == "group" else group,
        ident() if identity == "user" else user,
        when + timedelta(seconds=30),
        [{"type": "text", "data": {"text": "合成后续推广"}}],
    )
    if identity == "account":
        payload["self_id"] = 10000002
        async with SessionLocal() as session:
            current = await pipeline.run_pipeline(
                payload,
                session,
                message_source=OneBotMessageSource(),
                ai_service=SyntheticModels([ad_model()]),
                dedup_key=f"onebot:10000002:{payload['message_id']}",
            )
    else:
        current = await execute(payload, SyntheticModels([ad_model()]))
    assert (await reload_persisted(current)).verdict == "violation_high"


@pytest.mark.parametrize("category", ["porn", "violence", "flood"])
async def test_mixed_followup_keeps_severe_and_flood(monkeypatch, tmp_path, category):
    from app.moderation.rules import TextRuleEngine

    _, group, user, when = await mixed_source(monkeypatch, tmp_path)
    (tmp_path / "next.png").touch()
    results = [ad_model("mixed", category if category != "flood" else "ad")]
    if category == "violence":
        results = [
            results[0].model_copy(update={"review_role": "primary", "review_group": "same-image"}),
            results[0].model_copy(
                update={
                    "review_role": "secondary",
                    "review_group": "same-image",
                    "model_id": "independent-secondary",
                }
            ),
        ]
    engine = TextRuleEngine()
    for index in range(3 if category == "flood" else 1):
        segments = image_segments("next.png")
        if category == "flood":
            segments[0] = {"type": "text", "data": {"text": "合成推广 加我微信 synthetic001"}}
        current = await execute(
            event(
                group,
                user,
                when + timedelta(seconds=30 + index),
                segments,
                ("next.png",),
            ),
            SyntheticModels(results),
            engine,
        )
    current = await reload_persisted(current)
    if category == "flood":
        assert any(h["rule_id"] == "R005" for h in json.loads(current.detail_json)["rule_hits"])
    assert current.verdict == "violation_high"
    assert "recall" in json.loads(current.detail_json)["recommended_actions"]


@pytest.mark.parametrize(
    "extra",
    [
        [{"type": "unknown_custom", "data": {}}],
        [{"type": "forward", "data": {"id": "synthetic"}}],
        [json_segment({"app": "com.tencent.qun.share", "view": "group", "title": "合成群卡"})],
        [{"type": "record", "data": {"file": "missing.voice"}}],
        [{"type": "video", "data": {"file": "missing.mp4"}}],
        [{"type": "file", "data": {"file": "missing.pdf"}}],
        [{"type": "image", "data": {"file": "missing.gif"}}],
    ],
)
async def test_unsafe_mixed_source_cannot_grant_window(monkeypatch, tmp_path, extra):
    row, group, user, when = await mixed_source(monkeypatch, tmp_path, extra=extra)
    assert not json.loads(row.detail_json).get("campus_source_policy")
    current = await execute(
        event(
            group,
            user,
            when + timedelta(seconds=30),
            [{"type": "text", "data": {"text": "合成后续推广"}}],
        ),
        SyntheticModels([ad_model()]),
    )
    assert (await reload_persisted(current)).verdict == "violation_high"


async def test_missing_image_never_grants_mixed_window(monkeypatch, tmp_path):
    row, *_ = await mixed_source(monkeypatch, tmp_path, missing=True)
    assert row.verdict == "record_only"
    assert not json.loads(row.detail_json).get("campus_source_policy")


async def test_legacy_mixed_row_without_receipt_is_not_promoted(monkeypatch, tmp_path):
    row, group, user, when = await mixed_source(monkeypatch, tmp_path)
    async with SessionLocal() as session:
        saved = await session.scalar(select(ShadowDecision).where(ShadowDecision.id == row.id))
        detail = json.loads(saved.detail_json)
        detail.pop("campus_source_policy", None)
        detail["ai_results"][0].update(
            category=None,
            campus_wall_source="万能校园墙",
            evidence="校园墙白名单|文案:万能校园墙 合成活动",
        )
        saved.detail_json = json.dumps(detail, ensure_ascii=False)
        await session.commit()
    current = await execute(
        event(
            group,
            user,
            when + timedelta(seconds=30),
            [{"type": "text", "data": {"text": "合成后续推广"}}],
        ),
        SyntheticModels([ad_model()]),
    )
    assert (await reload_persisted(current)).verdict == "violation_high"


@pytest.mark.parametrize("pending_kind", ["active", "inbox"])
async def test_pending_mixed_source_protects_without_granting(pending_kind):
    from app.runtime.inbox import enqueue_event
    from app.runtime.pairing_context import (
        discard_active_pairing_message,
        register_active_pairing_message,
    )

    group, user = ident(), ident()
    when = datetime.now(UTC).replace(microsecond=0)
    source_payload = event(group, user, when, image_segments())
    source = OneBotMessageSource().parse_group_message(source_payload)
    key = f"onebot:10000001:{source_payload['message_id']}"
    token = object()
    if pending_kind == "active":
        register_active_pairing_message(token, source, key)
    else:
        async with SessionLocal() as session:
            await enqueue_event(session, source_payload, max_pending=100000)
    try:
        current = await execute(
            event(
                group,
                user,
                when + timedelta(seconds=30),
                [{"type": "text", "data": {"text": "合成后续推广"}}],
            ),
            SyntheticModels([ad_model()]),
        )
        current = await reload_persisted(current)
        assert current.verdict == "record_only"
        assert "未授予" in current.reason
        assert json.loads(current.detail_json)["recommended_actions"] == []
    finally:
        discard_active_pairing_message(token)


async def test_mixed_followup_does_not_extend_source_window(monkeypatch, tmp_path):
    _, group, user, when = await mixed_source(monkeypatch, tmp_path)
    (tmp_path / "next.png").touch()
    middle = await execute(
        event(
            group, user, when + timedelta(seconds=100), image_segments("next.png"), ("next.png",)
        ),
        SyntheticModels([ad_model("mixed")]),
    )
    middle = await reload_persisted(middle)
    assert middle.verdict == "record_only"
    assert not json.loads(middle.detail_json).get("campus_source_policy")
    later = await execute(
        event(
            group,
            user,
            when + timedelta(seconds=130),
            [{"type": "text", "data": {"text": "合成后续推广"}}],
        ),
        SyntheticModels([ad_model()]),
    )
    assert (await reload_persisted(later)).verdict == "violation_high"


@pytest.mark.parametrize(
    "segments,media",
    [
        ([], ["image/jpeg"]),
        ([{"kind": "text"}, {"kind": "image", "attachment_index": True}], ["image/jpeg"]),
        ([{"kind": "text"}, {"kind": "image", "attachment_index": -1}], ["image/jpeg"]),
        ([{"kind": "text"}, {"kind": "image", "attachment_index": 1}], ["image/jpeg"]),
        ([{"kind": "text"}, {"kind": "image", "attachment_index": 0}], ["image/jpeg", "image/png"]),
        (
            [{"kind": "text", "attachment_index": 0}, {"kind": "image", "attachment_index": 0}],
            ["image/jpeg"],
        ),
        ([{"kind": "text"}, {"kind": "image", "attachment_index": 0}], ["image/gif"]),
        ([{"kind": "text"}, {"kind": "image", "attachment_index": 0}], ["video/mp4"]),
        (
            [{"kind": "text"}, {"kind": "share_card"}, {"kind": "image", "attachment_index": 0}],
            ["image/jpeg"],
        ),
    ],
)
def test_ambiguous_mixed_shape_rejected(segments, media):
    from app.moderation.campus_message import plain_text_image_shape

    assert not plain_text_image_shape(segments, media)


def test_active_mixed_summary_preserves_only_structure():
    from app.moderation.campus_message import is_plain_text_image_message
    from app.runtime.pairing_context import _minimal_message

    payload = event(ident(), ident(), datetime.now(UTC), image_segments())
    original = OneBotMessageSource().parse_group_message(payload)
    minimal = _minimal_message(original, f"onebot:10000001:{original.message_id}")
    assert minimal.kind == "mixed" and is_plain_text_image_message(minimal)
    assert minimal.text == "" and all(s.text == "" for s in minimal.segments)
    assert all(not a.filename and not a.url for a in minimal.attachments)


async def test_completed_mixed_source_requires_its_structural_evidence(monkeypatch, tmp_path):
    row, group, user, when = await mixed_source(monkeypatch, tmp_path)
    async with SessionLocal() as session:
        saved = await session.scalar(select(ShadowDecision).where(ShadowDecision.id == row.id))
        detail = json.loads(saved.detail_json)
        assert detail["campus_source_policy"]
        detail.pop("segments")
        saved.detail_json = json.dumps(detail, ensure_ascii=False)
        await session.commit()
    current = await execute(
        event(
            group,
            user,
            when + timedelta(seconds=30),
            [{"type": "text", "data": {"text": "合成后续推广"}}],
        ),
        SyntheticModels([ad_model()]),
    )
    assert (await reload_persisted(current)).verdict == "violation_high"
