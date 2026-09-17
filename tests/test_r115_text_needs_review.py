"""主审 ddeb893 复验包 T1 探针正式化为仓库回归（纯文字 needs_review 转人工）。

来源：主审复验包 `test_text_null_followup.py`（受审 SHA ddeb893；修复前
2 failed / 2 passed——文字模型 category=None + needs_review=True 时，真实
`run_pipeline` 输出 allow、未进入人工处理；5060361 基线同样复现，属旧缺陷，
非 R1–R3 引入）。

核心断言（主审最小关闭标准第 2 条）：
- needs_review=True：verdict=record_only、recommended_actions 为空；
- needs_review=False（控制组）：verdict=allow、recommended_actions 为空；
- 白名单开/关两组均验证。
全部模型为固定替身；合成输入；不读生产数据、不外呼。
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIModerationResult, AIReviewService
from app.moderation.allowlist import add_term, delete_term
from app.runtime.pipeline import run_pipeline


class FixedText:
    def __init__(self, category, confidence, needs_review):
        self.model_id = "t1-text-" + uuid.uuid4().hex
        self.prompt_version = "synthetic-t1-text"
        self.category = category
        self.confidence = confidence
        self.needs_review = needs_review

    async def moderate_text(self, request):
        return AIModerationResult(
            model_id=self.model_id,
            category=self.category,
            confidence=self.confidence,
            needs_review=self.needs_review,
            evidence="synthetic; no external call",
        )


@pytest.mark.parametrize("white", [False, True])
@pytest.mark.parametrize("needs_review", [False, True])
async def test_null_text_review_boundary(white, needs_review):
    token = "合成文字边界" + uuid.uuid4().hex
    msg = StandardMessage(
        provider="onebot",
        message_id="text-null-" + uuid.uuid4().hex,
        external_group_id=str(int(uuid.uuid4().hex[:12], 16)),
        sender=Sender(member_openid="10000000002", role="member"),
        kind="text",
        text=token,
    )

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    ai = AIReviewService(
        enabled=True,
        enabled_groups={msg.external_group_id},
        text_moderator=FixedText(category=None, confidence=0.99, needs_review=needs_review),
    )
    async with SessionLocal() as session:
        row = None
        if white:
            row, _ = await add_term(session, token, operator="synthetic-review-t1")
        try:
            record = await run_pipeline(
                {"message_id": msg.message_id},
                session,
                message_source=Source(),
                ai_service=ai,
            )
            assert record is not None
            detail = json.loads(record.detail_json)
            assert record.verdict == ("record_only" if needs_review else "allow")
            assert detail["recommended_actions"] == []
        finally:
            if row is not None:
                await delete_term(session, row.id, operator="synthetic-review-t1")
