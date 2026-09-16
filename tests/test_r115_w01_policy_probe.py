"""D-033 广告白名单边界正式回归（R-115 W01 整改）。

来源：主审 R-115 探针 `qqbot-r115-policy-probe.py`（40 个合成场景）
正式化为仓库回归。核心断言：
- 白名单（或任何放行）**不得遮蔽** fraud/porn/violence/flood 与未完成图片审核；
- 只有广告内容才获得放行；
- 管理员保护、内置严重词、内置刷屏照常（B-2 底线不受本整改影响）。

修复前（58344a6）：16 failed / 24 passed；本文件在整改后应全绿。
所有模型返回都是固定替身；图片为合成 1px PNG；不读生产数据、不外呼。
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
from app.moderation.dynamic_rules import add_rule_item, create_rule_draft, publish_rule_version
from app.moderation.image_engine import MediaAnalysis
from app.runtime.pipeline import run_pipeline


class FixedModel:
    def __init__(self, category, confidence, needs_review=False):
        self.model_id = "synthetic-" + uuid.uuid4().hex
        self.prompt_version = "synthetic-d033"
        self.category = category
        self.confidence = confidence
        self.needs_review = needs_review
        self.calls = 0

    async def moderate_text(self, request):
        self.calls += 1
        return AIModerationResult(
            model_id=self.model_id,
            category=self.category,
            confidence=self.confidence,
            needs_review=self.needs_review,
            evidence="synthetic non-ad evidence",
        )

    async def moderate_image(self, request):
        return await self.moderate_text(request)


class NeutralImage:
    def __init__(self, unknown=False):
        self.unknown = unknown

    def analyze(self, path):
        return MediaAnalysis(
            "record_only" if self.unknown else "allow",
            0.3 if self.unknown else 0.99,
            reason="synthetic unreviewed image" if self.unknown else "synthetic neutral image",
        )


async def probe(monkeypatch, tmp_path, path, category, allowlisted, role="member"):
    import app.runtime.pipeline as pipeline

    unique = "合成标记" + uuid.uuid4().hex
    # 不含办证/学历、校园墙、本地硬黑名单词——隔离 D-031/D-032 独立政策。
    msg = StandardMessage(
        provider="onebot",
        message_id="probe-" + uuid.uuid4().hex,
        external_group_id=str(int(uuid.uuid4().hex[:12], 16)),
        sender=Sender(member_openid="10000000002", role=role),
        kind="mixed" if path in ("vision-pair", "media-unknown") else "text",
        text=unique,
        attachments=[Attachment(content_type="image/png", filename="synthetic.png")]
        if path in ("vision-pair", "media-unknown")
        else [],
    )
    # 自包含 1px PNG；图像分析器与模型均为固定替身。
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

    primary = FixedModel(
        category, 0.4 if path == "text-low" else 0.99, path in ("text-low", "vision-pair")
    )
    secondary = FixedModel(category, 0.99)
    ai = AIReviewService(
        enabled=path in ("text-high", "text-low", "vision-pair"),
        enabled_groups={msg.external_group_id},
        text_moderator=primary if path in ("text-high", "text-low") else None,
        vision_moderator=primary if path == "vision-pair" else None,
        review_vision_moderator=secondary if path == "vision-pair" else None,
    )
    async with SessionLocal() as session:
        if path == "dynamic":
            draft = await create_rule_draft(
                session, scope="group", scope_key=msg.external_group_id, name="synthetic D033"
            )
            await add_rule_item(
                session,
                draft.id,
                item_type="keyword",
                pattern=unique,
                category=category,
                weight=0.95,
            )
            await publish_rule_version(session, draft.id, operator="synthetic-review")
        row = None
        if allowlisted:
            row, _ = await add_term(session, unique, operator="synthetic-review")
        try:
            record = await run_pipeline(
                {"message_id": msg.message_id},
                session,
                message_source=Source(),
                ai_service=ai,
                image_engine=NeutralImage(path == "media-unknown"),
            )
            assert record is not None
            detail = json.loads(record.detail_json)
            return {
                "path": path,
                "category_requested": category,
                "allowlisted": allowlisted,
                "role": role,
                "verdict": record.verdict,
                "category": record.category,
                "recommended_actions": detail["recommended_actions"],
                "ai_categories": [r["category"] for r in detail["ai_results"]],
                "hit_categories": [r["category"] for r in detail["rule_hits"]],
                "model_calls": [primary.calls, secondary.calls],
                "protected": detail["is_protected_sender"],
                "evidence_vetoes": detail["evidence_vetoes"],
            }
        finally:
            if row is not None:
                await delete_term(session, row.id, operator="synthetic-review")


CASES = (
    [("dynamic", c) for c in ("fraud", "porn", "violence", "flood")]
    + [("text-high", c) for c in ("fraud", "porn", "violence", "flood")]
    + [("text-low", c) for c in ("fraud", "porn", "violence")]
    + [("vision-pair", c) for c in ("fraud", "porn", "violence", "flood")]
    + [("media-unknown", None)]
)


@pytest.mark.parametrize("allowlisted", [False, True])
@pytest.mark.parametrize("path,category", CASES)
async def test_non_ad_evidence_must_not_be_allowed(
    monkeypatch, tmp_path, path, category, allowlisted
):
    """非广告证据（白名单开或关）一律不得 allow——W01 核心断言。"""
    result = await probe(monkeypatch, tmp_path, path, category, allowlisted)
    assert result["verdict"] != "allow", result
    assert not result["protected"]


@pytest.mark.parametrize("path", ["dynamic", "text-high"])
async def test_ad_is_allowlisted(monkeypatch, tmp_path, path):
    """纯广告内容仍然放行（白名单本意）。"""
    result = await probe(monkeypatch, tmp_path, path, "ad", True)
    assert result["verdict"] == "allow"
    assert result["recommended_actions"] == []


@pytest.mark.parametrize("allowlisted", [False, True])
async def test_protected_sender_is_not_punished(monkeypatch, tmp_path, allowlisted):
    """管理员/群主发送者不被处罚（D-032 独立政策不受本整改影响）。"""
    result = await probe(
        monkeypatch,
        tmp_path,
        path="text-high",
        category="fraud",
        allowlisted=allowlisted,
        role="admin",
    )
    assert result["verdict"] == "record_only"
    assert result["recommended_actions"] == []
    assert result["protected"]


@pytest.mark.parametrize("severe_word", ["博彩", "裸聊", "枪支"])
def test_builtin_severe_guard_still_works(severe_word):
    from app.moderation.rules import TextRuleEngine

    msg = StandardMessage(
        provider="onebot",
        message_id="builtin-" + uuid.uuid4().hex,
        external_group_id="synthetic-local-group",
        sender=Sender(member_openid="synthetic-local-member", role="member"),
        kind="text",
        text="合成标记" + severe_word,
    )
    result = TextRuleEngine(allow_terms=frozenset({"合成标记"})).evaluate(msg)
    assert result.verdict == "violation_high"


def test_builtin_flood_guard_still_works():
    from app.moderation.rules import TextRuleEngine

    engine = TextRuleEngine(allow_terms=frozenset({"合成标记"}))
    verdicts = []
    for _ in range(3):
        msg = StandardMessage(
            provider="onebot",
            message_id="builtin-" + uuid.uuid4().hex,
            external_group_id="synthetic-local-group",
            sender=Sender(member_openid="synthetic-local-member", role="member"),
            kind="text",
            text="合成标记",
        )
        verdicts.append(engine.evaluate(msg).verdict)
    assert verdicts == ["allow", "allow", "violation_high"]
