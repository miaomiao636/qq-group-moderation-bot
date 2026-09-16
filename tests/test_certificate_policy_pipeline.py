"""R-108: a local certificate exception cannot be undone by an AI ad result."""

from __future__ import annotations

import json
import uuid

import pytest
from app.core.contracts import Attachment, Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIReviewService, merge_ai_evidence
from app.moderation.decision import CERTIFICATE_AD_ALLOW_RULE_ID
from app.moderation.dynamic_rules import (
    add_rule_item,
    create_rule_draft,
    load_active_snapshot,
    publish_rule_version,
)
from app.moderation.image_engine import MediaAnalysis
from app.moderation.rules import TextRuleEngine
from app.runtime.pipeline import run_pipeline


class FakeAd:
    model_id = "synthetic-certificate-ad"
    prompt_version = "synthetic-certificate-review"

    async def moderate_text(self, request):
        return AIModerationResult(
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            category="ad",
            confidence=0.99,
            needs_review=False,
            evidence="synthetic ad classification",
        )

    async def moderate_image(self, request):
        return await self.moderate_text(request)


def _message(text: str) -> StandardMessage:
    return StandardMessage(
        message_id="certificate-" + uuid.uuid4().hex,
        provider="onebot",
        external_group_id="certificate-group-" + uuid.uuid4().hex,
        sender=Sender(member_openid="synthetic-member"),
        kind="text",
        text=text,
    )


@pytest.mark.parametrize("source", ["text", "vision"])
async def test_certificate_ad_exception_survives_real_ai_pipeline(tmp_path, monkeypatch, source):
    msg = _message("代做学历证书，学信网可查，加我微信 synthetic_contact")

    class NeutralImage:
        def analyze(self, path):
            return MediaAnalysis("record_only", 0.1, reason="synthetic image")

    if source == "vision":
        (tmp_path / "synthetic.png").write_bytes(b"synthetic-image-not-decoded")
        msg = msg.model_copy(
            update={
                "kind": "mixed",
                "attachments": [Attachment(content_type="image/png", filename="synthetic.png")],
            }
        )
        monkeypatch.setattr("app.runtime.pipeline.MEDIA_DIR", tmp_path)

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    model = FakeAd()
    ai = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        text_moderator=model if source == "text" else None,
        vision_moderator=model if source == "vision" else None,
    )
    async with SessionLocal() as session:
        record = await run_pipeline(
            {"message_id": msg.message_id},
            session,
            message_source=Source(),
            image_engine=NeutralImage(),
            ai_service=ai,
        )
    assert record is not None
    assert record.verdict == "allow"  # 2026-09-16 口径 A：完全放行（AI ad 0.99 不撤销豁免）
    detail = json.loads(record.detail_json)
    assert detail["recommended_actions"] == []
    assert detail["ai_results"]


@pytest.mark.parametrize("category", ["ad", "fraud", "porn", "violence"])
async def test_explicit_dynamic_block_is_not_disabled_by_certificate_marker(category):
    msg = _message("办证 定向合成阻断词")
    async with SessionLocal() as session:
        draft = await create_rule_draft(
            session, scope="group", scope_key=msg.external_group_id, name="synthetic certificate"
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="定向合成阻断词",
            category=category,
            weight=0.99,
        )
        await publish_rule_version(session, draft.id, operator="synthetic-admin")
        snapshot = await load_active_snapshot(session, msg.external_group_id)
    result = TextRuleEngine(rule_snapshot=snapshot).evaluate(msg)
    assert result.verdict == "violation_high"
    assert result.category == category
    assert any(hit.rule_id.startswith("DR_") for hit in result.rule_hits)


def test_certificate_word_does_not_exempt_explicit_severe_local_content():
    result = TextRuleEngine().evaluate(_message("办证并推广裸聊，加我微信 synthetic_contact"))
    assert result.category == "porn"
    assert result.verdict == "violation_high"


@pytest.mark.parametrize("certificate_prefix", ["", "办证并"])
@pytest.mark.parametrize(
    "severe_word,expected_category",
    [
        ("赌球", "fraud"),
        ("网赌", "fraud"),
        ("博彩", "fraud"),
        ("枪支", "violence"),
        ("弹药", "violence"),
        ("毒品", "violence"),
        ("麻古", "violence"),
        ("冰粉", "violence"),
    ],
)
def test_ad_keyword_cannot_hide_a_severe_blacklist_category(
    certificate_prefix, severe_word, expected_category
):
    # The built-in blacklist lists the ad phrase before every severe word. Its
    # iteration order must not turn severe content into certificate-exempt ads.
    msg = _message(f"{certificate_prefix}推广{severe_word}，加我微信 synthetic_contact")
    result = TextRuleEngine().evaluate(msg)
    assert result.category == expected_category
    assert result.verdict == "violation_high"
    assert result.recommended_actions
    assert not any(hit.rule_id == CERTIFICATE_AD_ALLOW_RULE_ID for hit in result.rule_hits)


@pytest.mark.parametrize("extra_severe", ["博彩", "枪支"])
def test_severe_category_scan_preserves_existing_porn_precedence(extra_severe):
    result = TextRuleEngine().evaluate(
        _message(f"办证并推广裸聊{extra_severe}，加我微信 synthetic_contact")
    )
    assert result.category == "porn"
    assert result.verdict == "violation_high"


@pytest.mark.parametrize("category", ["ad", "fraud", "porn"])
def test_independent_ai_pair_respects_only_the_certificate_ad_exception(category):
    """2026-09-16 口径 A：办证完全放行——独立 AI 二审（含严重类别确认）
    不得升级或转人工（仅记录证据）。"""
    local = TextRuleEngine().evaluate(_message("办证，加我微信 synthetic_contact"))
    results = [
        AIModerationResult(
            model_id=model_id,
            source="vision",
            review_role=role,
            review_group="synthetic-image",
            category=category,
            confidence=0.99,
            needs_review=False,
            evidence="synthetic independent review",
        )
        for model_id, role in (("synthetic-primary", "primary"), ("synthetic-review", "secondary"))
    ]
    decision = merge_ai_evidence(local, results)
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_certificate_marker_text_is_not_a_policy_permission():
    local = TextRuleEngine().evaluate(_message(CERTIFICATE_AD_ALLOW_RULE_ID))
    assert not any(hit.rule_id == CERTIFICATE_AD_ALLOW_RULE_ID for hit in local.rule_hits)
    result = AIModerationResult(
        model_id="synthetic-ad",
        source="text",
        category="ad",
        confidence=0.99,
        needs_review=False,
        evidence=CERTIFICATE_AD_ALLOW_RULE_ID,
    )
    assert merge_ai_evidence(local, [result]).verdict == "violation_high"
