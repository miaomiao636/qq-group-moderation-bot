"""主审 5060361 复验包探针（R3：D-031 办证 × D-033 白名单重叠）正式化为仓库回归。

来源：主审复验包 `test_certificate_dynamic_overlap.py`（修复前 .2 权重
3/3 失败）。修复目标：全部动态命中均为办证类词触发时，D-031 办证保护
先于 D-033 弱信号分支生效——三类别 × 0.2/0.95 × 白名单开/关共 12 项
全部 allow 且建议为空。合成数据，不读生产数据、不外呼。
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.moderation.ai import AIReviewService
from app.moderation.allowlist import add_term, delete_term
from app.moderation.dynamic_rules import (
    add_rule_item,
    create_rule_draft,
    publish_rule_version,
)
from app.runtime.pipeline import run_pipeline


@pytest.mark.parametrize("allowlisted", [False, True])
@pytest.mark.parametrize("weight", [0.2, 0.95])
@pytest.mark.parametrize("category", ["fraud", "porn", "violence"])
async def test_certificate_dr_full_allow_survives_allowlist(allowlisted, weight, category):
    token = "合成案例" + uuid.uuid4().hex
    msg = StandardMessage(
        provider="onebot",
        message_id="probe-" + uuid.uuid4().hex,
        external_group_id=str(int(uuid.uuid4().hex[:12], 16)),
        sender=Sender(member_openid="synthetic-member", role="member"),
        kind="text",
        text="办证 " + token,
    )

    class Source:
        provider = "onebot"

        def parse_group_message(self, payload):
            return msg

    async with SessionLocal() as session:
        draft = await create_rule_draft(
            session,
            scope="group",
            scope_key=msg.external_group_id,
            name="synthetic certificate",
        )
        await add_rule_item(
            session,
            draft.id,
            item_type="keyword",
            pattern="办证",
            category=category,
            weight=weight,
        )
        await publish_rule_version(session, draft.id, operator="synthetic-review")
        row = None
        if allowlisted:
            row, _ = await add_term(session, token, operator="synthetic-review")
        try:
            result = await run_pipeline(
                {"message_id": msg.message_id},
                session,
                message_source=Source(),
                ai_service=AIReviewService(enabled=False, enabled_groups=set()),
            )
            detail = json.loads(result.detail_json)
            actual = {
                "white": allowlisted,
                "weight": weight,
                "requested": category,
                "verdict": result.verdict,
                "category": result.category,
                "confidence": result.confidence,
                "actions": detail["recommended_actions"],
                "hits": [(h["rule_id"], h["category"]) for h in detail["rule_hits"]],
            }
            print(json.dumps(actual, ensure_ascii=False))
            assert result.verdict == "allow", actual
            assert detail["recommended_actions"] == [], actual
        finally:
            if row is not None:
                await delete_term(session, row.id, operator="synthetic-review")
