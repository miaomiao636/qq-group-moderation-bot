# ruff: noqa: E402, I001, F401
# Reviewer probe pack (PR #45 / r132), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.

import json
import uuid

import pytest

from app.adapters.onebot.parser import OneBotMessageSource
from app.db import SessionLocal
from app.moderation.allowlist import add_member, delete_member
from app.moderation.decision import GROUP_CARD_RECALL_RULE_ID
from app.moderation.rules import TextRuleEngine
from app.runtime.pipeline import run_pipeline


def payload(segments, role="member", user_id="900000777"):
    return {
        "post_type": "message",
        "message_type": "group",
        "message_id": "r132-" + uuid.uuid4().hex,
        "group_id": "synthetic-" + uuid.uuid4().hex,
        "user_id": user_id,
        "sender": {"role": role},
        "message": segments,
    }


def json_segment(card):
    return {"type": "json", "data": {"data": json.dumps(card, ensure_ascii=False)}}


GROUP = {
    "app": "com.tencent.qun.share",
    "view": "group",
    "prompt": "[群名片]研究交流",
    "meta": {"group": {"groupCode": "900000001", "groupName": "研究交流"}},
}


@pytest.mark.parametrize(
    "prefix", [[], [{"type": "text", "data": {"text": "看看"}}]], ids=["bare", "with-text"]
)
async def test_group_card_structural_rule_survives_mixed_text(prefix):
    raw = payload(prefix + [json_segment(GROUP)])
    msg = OneBotMessageSource().parse_group_message(raw)
    assert msg.share_card.is_group_card is True
    async with SessionLocal() as session:
        row = await run_pipeline(raw, session, message_source=OneBotMessageSource())
    assert row is not None
    detail = json.loads(row.detail_json)
    assert row.verdict == "violation_high", (msg.kind, row.verdict, row.reason)
    assert GROUP_CARD_RECALL_RULE_ID in {h["rule_id"] for h in detail["rule_hits"]}


@pytest.mark.parametrize(
    "card",
    [
        {
            "app": "com.tencent.news",
            "title": "科技新闻",
            "prompt": "[新闻]新版 QQ 群名片设置使用教程",
        },
        {"app": "com.tencent.music", "title": "夜曲", "prompt": "[音乐]推荐群友听一首好歌"},
    ],
    ids=["news-mentioned-group-card", "music-ordinary-phrase"],
)
async def test_descriptive_text_does_not_become_structural_group_card(card):
    raw = payload([json_segment(card)])
    msg = OneBotMessageSource().parse_group_message(raw)
    async with SessionLocal() as session:
        row = await run_pipeline(raw, session, message_source=OneBotMessageSource())
    assert row is not None
    assert row.verdict != "violation_high", (msg.share_card, row.reason)
    assert GROUP_CARD_RECALL_RULE_ID not in {
        h["rule_id"] for h in json.loads(row.detail_json)["rule_hits"]
    }


@pytest.mark.parametrize(
    "segments",
    [
        [{"type": "unknown_synthetic", "data": {"synthetic": "yes"}}],
        [{"type": "image", "data": {"file": "missing-synthetic.png"}}],
    ],
    ids=["unknown-segment", "missing-image"],
)
async def test_member_policy_allow_not_downgraded_by_unavailable_content(segments):
    user_id = "900000778"
    raw = payload(segments, user_id=user_id)
    msg = OneBotMessageSource().parse_group_message(raw)
    local = TextRuleEngine(allow_members=frozenset({("onebot", user_id)})).evaluate(msg)
    assert local.verdict == "allow"
    async with SessionLocal() as session:
        member, created = await add_member(session, user_id, operator="test:independent")
        try:
            row = await run_pipeline(raw, session, message_source=OneBotMessageSource())
        finally:
            if created:
                await delete_member(session, member.id, operator="test:independent")
    assert row is not None
    assert row.verdict == "allow", (row.verdict, row.reason)
    assert json.loads(row.detail_json)["recommended_actions"] == []


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_structural_group_card_protects_roles(role):
    raw = payload([json_segment(GROUP)], role=role)
    async with SessionLocal() as session:
        row = await run_pipeline(raw, session, message_source=OneBotMessageSource())
    assert row is not None
    assert row.verdict == "allow"
    assert json.loads(row.detail_json)["recommended_actions"] == []
