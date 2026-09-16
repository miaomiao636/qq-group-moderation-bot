"""主审 5060361 复验包探针（R1/R2 边界面）正式化为仓库回归。

来源：主审复验包 `test_c02_c03_edges.py`（受审 SHA 5060361；修复前
24 项中 11 failed / 13 passed）。核心断言：
- R1：category=None 且任一模型 needs_review=True 时，白名单不得提前放行
  （转人工 record_only、建议为空）；纯 None（needs_review=False、未降级）
  的"确定性正常"控制组仍可放行；
- R2：白名单命中时，弱/强非广告证据的 category/confidence 必须由非广告
  证据同源产生（不得混入广告分、不得丢失类别）。
全部模型返回为固定替身；图片为合成 1px PNG；不读生产数据、不外呼。
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from app.core.contracts import Attachment, Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIReviewService
from app.moderation.allowlist import add_term, delete_term
from app.moderation.dynamic_rules import (
    add_rule_item,
    create_rule_draft,
    publish_rule_version,
)
from app.moderation.image_engine import MediaAnalysis
from app.runtime.pipeline import run_pipeline


class Fixed:
    def __init__(self, category=None, confidence=0.99, needs_review=False):
        self.model_id = "edge-" + uuid.uuid4().hex
        self.prompt_version = "synthetic-c02-c03"
        self.category = category
        self.confidence = confidence
        self.needs_review = needs_review

    async def moderate_text(self, request):
        return AIModerationResult(
            model_id=self.model_id,
            category=self.category,
            confidence=self.confidence,
            needs_review=self.needs_review,
            evidence="synthetic",
        )

    async def moderate_image(self, request):
        return await self.moderate_text(request)


class NeutralImage:
    def analyze(self, path):
        return MediaAnalysis("allow", 0.99, reason="synthetic neutral image")


async def run_case(
    monkeypatch,
    tmp_path,
    *,
    allowlisted=True,
    primary_needs=False,
    secondary_needs=False,
    dr_weight=None,
    severe_first=False,
    ad_ai=False,
):
    import app.runtime.pipeline as pipeline

    token = "合成边界" + uuid.uuid4().hex
    is_image = dr_weight is None
    msg = StandardMessage(
        provider="onebot",
        message_id="edge-" + uuid.uuid4().hex,
        external_group_id=str(int(uuid.uuid4().hex[:12], 16)),
        sender=Sender(member_openid="10000000002", role="member"),
        kind="mixed" if is_image else "text",
        text=token,
        attachments=[Attachment(content_type="image/png", filename="synthetic.png")]
        if is_image
        else [],
    )
    (tmp_path / "synthetic.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRZ8AAAAASUVORK5CYII="
        )
    )
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    ai = AIReviewService(
        enabled=is_image or ad_ai,
        enabled_groups={msg.external_group_id},
        text_moderator=Fixed("ad") if ad_ai else None,
        vision_moderator=Fixed(needs_review=primary_needs) if is_image else None,
        review_vision_moderator=Fixed(needs_review=secondary_needs) if is_image else None,
    )
    async with SessionLocal() as session:
        if dr_weight is not None:
            draft = await create_rule_draft(
                session,
                scope="group",
                scope_key=msg.external_group_id,
                name="synthetic DR",
            )
            rules = [("ad", 0.8), ("fraud", dr_weight)]
            if severe_first:
                rules.reverse()
            for category, weight in rules:
                await add_rule_item(
                    session,
                    draft.id,
                    item_type="keyword",
                    pattern=token,
                    category=category,
                    weight=weight,
                )
            await publish_rule_version(session, draft.id, operator="synthetic-review")
        row = None
        if allowlisted:
            row, _ = await add_term(session, token, operator="synthetic-review")
        try:
            record = await run_pipeline(
                {"message_id": msg.message_id},
                session,
                message_source=Source(),
                ai_service=ai,
                image_engine=NeutralImage(),
            )
            assert record is not None
            detail = json.loads(record.detail_json)
            result = {
                "allowlisted": allowlisted,
                "primary_needs": primary_needs,
                "secondary_needs": secondary_needs,
                "dr_weight": dr_weight,
                "severe_first": severe_first,
                "ad_ai": ad_ai,
                "verdict": record.verdict,
                "category": record.category,
                "confidence": record.confidence,
                "actions": detail["recommended_actions"],
                "ai": [
                    (
                        r["category"],
                        r["confidence"],
                        r["needs_review"],
                        r["review_role"],
                        r["degraded_reason"],
                    )
                    for r in detail["ai_results"]
                ],
                "hits": [
                    (r["rule_id"], r["category"], r["confidence_delta"])
                    for r in detail["rule_hits"]
                ],
            }
            print(json.dumps(result, ensure_ascii=False))
            return result
        finally:
            if row is not None:
                await delete_term(session, row.id, operator="synthetic-review")


@pytest.mark.parametrize("allowlisted", [False, True])
@pytest.mark.parametrize("secondary_needs", [False, True])
async def test_null_primary_needs_review_must_not_vanish(
    monkeypatch, tmp_path, allowlisted, secondary_needs
):
    result = await run_case(
        monkeypatch,
        tmp_path,
        allowlisted=allowlisted,
        primary_needs=True,
        secondary_needs=secondary_needs,
    )
    assert result["verdict"] == "record_only"
    assert result["actions"] == []


@pytest.mark.parametrize("allowlisted", [False, True])
async def test_normal_result_preserves_existing_media_baseline(monkeypatch, tmp_path, allowlisted):
    result = await run_case(monkeypatch, tmp_path, allowlisted=allowlisted)
    # The existing no-local-hit mixed-media pipeline remains record_only; it is
    # outside this review. D033 + confidently normal + needs_review=False allows.
    assert result["verdict"] == ("allow" if allowlisted else "record_only")


@pytest.mark.parametrize("ad_ai", [False, True])
@pytest.mark.parametrize("severe_first", [False, True])
async def test_weak_nonad_metadata_is_preserved(monkeypatch, tmp_path, ad_ai, severe_first):
    result = await run_case(
        monkeypatch,
        tmp_path,
        dr_weight=0.2,
        severe_first=severe_first,
        ad_ai=ad_ai,
    )
    assert result["verdict"] == "record_only"
    assert result["actions"] == []
    assert result["category"] == "fraud"
    assert result["confidence"] == pytest.approx(0.2)


@pytest.mark.parametrize("severe_first", [False, True])
async def test_strong_nonad_metadata_is_preserved(monkeypatch, tmp_path, severe_first):
    result = await run_case(
        monkeypatch,
        tmp_path,
        dr_weight=0.95,
        severe_first=severe_first,
    )
    assert result["verdict"] == "violation_high"
    assert result["category"] == "fraud"
    assert result["confidence"] == pytest.approx(0.95)
