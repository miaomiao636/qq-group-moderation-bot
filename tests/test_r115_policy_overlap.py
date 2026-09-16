# ruff: noqa: E402  (R-115 入库探针：保留原文件 import 顺序)
"""Independent review probe. Use README commands and the repository test bootstrap."""


# 入库说明（R-115 正式回归，来源：主审交付包）：原探针要求在仓库外运行并由外部
# conftest 引导；迁入 tests/ 后由 tests/conftest.py 自动提供隔离环境（临时迁移库 /
# SHADOW / 关闭真实模型与通知），因此不再做外部引导检查。

"""D-031/D-033 overlap: synthetic pipeline only, no external calls."""
import json
import uuid

import pytest
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIReviewService
from app.moderation.allowlist import add_term, delete_term
from app.runtime.pipeline import run_pipeline


@pytest.mark.parametrize("allowlisted", [False, True])
@pytest.mark.parametrize("category", ["fraud", "porn", "violence"])
@pytest.mark.parametrize("policy", ["certificate", "admin-card"])
async def test_independent_full_allow_policy_survives_ad_allowlist(allowlisted, category, policy):
    unique = "合成案例" + uuid.uuid4().hex
    msg = StandardMessage(
        provider="onebot",
        message_id="probe-" + uuid.uuid4().hex,
        external_group_id=str(int(uuid.uuid4().hex[:12], 16)),
        sender=Sender(
            member_openid="synthetic-member",
            role="admin" if policy == "admin-card" else "member",
        ),
        kind="share_card" if policy == "admin-card" else "text",
        text=unique if policy == "admin-card" else "办证 学历提升 " + unique,
    )

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    class FixedModel:
        model_id = "synthetic-" + uuid.uuid4().hex
        prompt_version = "synthetic-d031-overlap"

        async def moderate_text(self, request):
            return AIModerationResult(
                model_id=self.model_id,
                category=category,
                confidence=0.99,
                needs_review=False,
                evidence="synthetic severe classification",
            )

    ai = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        text_moderator=FixedModel(),
    )
    async with SessionLocal() as session:
        row = None
        if allowlisted:
            row, _ = await add_term(session, unique, operator="synthetic-review")
        try:
            record = await run_pipeline(
                {"message_id": msg.message_id},
                session,
                message_source=Source(),
                ai_service=ai,
            )
            detail = json.loads(record.detail_json)
            result = {
                "policy": policy,
                "allowlisted": allowlisted,
                "ai_category": category,
                "verdict": record.verdict,
                "category": record.category,
                "recommended_actions": detail["recommended_actions"],
                "rule_ids": [h["rule_id"] for h in detail["rule_hits"]],
            }
            print(json.dumps(result, ensure_ascii=False))
            assert record.verdict == "allow", result
            assert detail["recommended_actions"] == [], result
        finally:
            if row is not None:
                await delete_term(session, row.id, operator="synthetic-review")
