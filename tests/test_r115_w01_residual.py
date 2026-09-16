"""Independent review probe. Use README commands and the repository test bootstrap."""


# 入库说明（R-115 正式回归，来源：主审交付包）：原探针要求在仓库外运行并由外部
# conftest 引导；迁入 tests/ 后由 tests/conftest.py 自动提供隔离环境（临时迁移库 /
# SHADOW / 关闭真实模型与通知），因此不再做外部引导检查。

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
    def __init__(self, category=None, confidence=0.99, needs_review=False, fail=False):
        self.model_id = "fixed-" + uuid.uuid4().hex
        self.prompt_version = "residual-w01"
        self.category = category
        self.confidence = confidence
        self.needs_review = needs_review
        self.fail = fail

    async def moderate_text(self, request):
        if self.fail:
            raise RuntimeError("synthetic failure; no network")
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


@pytest.mark.parametrize("allowlisted", [False, True])
@pytest.mark.parametrize(
    "scenario",
    [
        "text-error",
        "config-error",
        "text-other",
        "vision-other",
        "mixed-ai",
        "mixed-dr-ad-first",
        "mixed-dr-severe-first",
    ],
)
async def test_policy_residual(monkeypatch, tmp_path, allowlisted, scenario):
    import app.runtime.pipeline as pipeline

    token = "合成案例" + uuid.uuid4().hex
    image = scenario in ("vision-other", "mixed-ai")
    msg = StandardMessage(
        provider="onebot",
        message_id="residual-" + uuid.uuid4().hex,
        external_group_id=str(int(uuid.uuid4().hex[:12], 16)),
        sender=Sender(member_openid="10000000002", role="member"),
        kind="mixed" if image else "text",
        text=token,
        attachments=[Attachment(content_type="image/png", filename="synthetic.png")]
        if image
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

    text = None
    vision = None
    secondary = None
    if scenario == "text-error":
        text = Fixed(fail=True)
    if scenario == "text-other":
        text = Fixed("other", needs_review=True)
    if scenario == "vision-other":
        vision = Fixed("other", needs_review=True)
        secondary = Fixed("other", needs_review=True)
    if scenario == "mixed-ai":
        text = Fixed("ad")
        vision = Fixed("fraud", 0.4, False)
    ai = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        text_moderator=text,
        vision_moderator=vision,
        review_vision_moderator=secondary,
        config_problem="synthetic_config_failure" if scenario == "config-error" else "",
    )
    async with SessionLocal() as s:
        if scenario.startswith("mixed-dr"):
            draft = await create_rule_draft(
                s,
                scope="group",
                scope_key=msg.external_group_id,
                name="synthetic mixed",
            )
            rules = [("ad", 0.8), ("fraud", 0.2)]
            if scenario == "mixed-dr-severe-first":
                rules.reverse()
            for category, weight in rules:
                await add_rule_item(
                    s,
                    draft.id,
                    item_type="keyword",
                    pattern=token,
                    category=category,
                    weight=weight,
                )
            await publish_rule_version(s, draft.id, operator="synthetic-review")
        row = None
        if allowlisted:
            row, _ = await add_term(s, token, operator="synthetic-review")
        try:
            record = await run_pipeline(
                {"message_id": msg.message_id},
                s,
                message_source=Source(),
                ai_service=ai,
                image_engine=NeutralImage(),
            )
            assert record is not None
            detail = json.loads(record.detail_json)
            print(
                json.dumps(
                    {
                        "scenario": scenario,
                        "white": allowlisted,
                        "verdict": record.verdict,
                        "category": record.category,
                        "confidence": record.confidence,
                        "actions": detail["recommended_actions"],
                        "ai": [
                            (
                                r["category"],
                                r["confidence"],
                                r["needs_review"],
                                r["degraded_reason"],
                            )
                            for r in detail["ai_results"]
                        ],
                        "hits": [
                            (r["rule_id"], r["category"], r["confidence_delta"])
                            for r in detail["rule_hits"]
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            if allowlisted or scenario in (
                "text-error",
                "config-error",
                "text-other",
                "vision-other",
            ):
                assert record.verdict == "record_only"
                assert detail["recommended_actions"] == []
            else:
                assert record.verdict == "violation_high"
        finally:
            if row is not None:
                await delete_term(s, row.id, operator="synthetic-review")
