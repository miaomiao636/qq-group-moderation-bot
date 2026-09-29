"""CARD-RECALL-20260923: authorized WeChat cards, never image QR inference."""

from __future__ import annotations

import json
import uuid
from copy import deepcopy

import pytest
from app.adapters.onebot.parser import OneBotMessageSource
from app.adapters.qq_official.parser import parse_group_message
from app.core.contracts import Sender, ShareCardInfo, StandardMessage
from app.moderation.decision import ModerationDecision, RuleHit
from app.moderation.review_gate import ReviewGate
from app.moderation.rules import TextRuleEngine
from app.moderation.wall_pair import is_pairing_candidate

RULE = "R_WECHAT_MINIPROGRAM_CARD"

# Structures verified with local QQ Ark generation / retained message lookup.
# All identity, display and link values below are synthetic; no production payload is stored.
WECHAT = {
    "app": "com.tencent.miniapp.lua",
    "view": "miniapp",
    "prompt": "[微信小程序]合成测试",
    "meta": {
        "miniapp": {"tag": "微信小程序", "tagIcon": "https://miniapp.gtimg.cn/public/miniwx.png"}
    },
}
GROUP = {
    "app": "com.tencent.contact.lua",
    "view": "contact",
    "prompt": "群名片: 合成测试",
    "meta": {
        "contact": {
            "nickname": "合成测试群",
            "jumpUrl": "mqqapi://card/show_pslcard?src_type=internal&version=1&uin=900000003&card_type=group",
        }
    },
}
NEWS = {"app": "com.tencent.news", "title": "微信小程序与群名片的使用教程"}


def payload(cards, *, role="member", prefix=None):
    return {
        "post_type": "message",
        "message_type": "group",
        "self_id": "10000001",
        "message_id": "synthetic-" + uuid.uuid4().hex,
        "group_id": "900000001",
        "user_id": "900000002",
        "sender": {"role": role},
        "message": (prefix or [])
        + [{"type": "json", "data": {"data": json.dumps(c)}} for c in cards],
    }


@pytest.mark.parametrize("card,rule", [(WECHAT, RULE), (GROUP, "R_GROUP_CARD")])
@pytest.mark.parametrize("order", ["single", "first", "last"])
def test_observed_card_structures_survive_other_cards(card, rule, order):
    cards = [card] if order == "single" else ([card, NEWS] if order == "first" else [NEWS, card])
    msg = OneBotMessageSource().parse_group_message(payload(cards))
    decision = ReviewGate().review(msg, TextRuleEngine().evaluate(msg))
    assert decision.verdict == "violation_high"
    assert rule in {h.rule_id for h in decision.rule_hits}


@pytest.mark.parametrize("cards", [[WECHAT, GROUP], [GROUP, WECHAT], [NEWS, WECHAT, GROUP]])
def test_two_structural_card_flags_are_both_retained(cards):
    msg = OneBotMessageSource().parse_group_message(payload(cards))
    assert msg.share_card.is_group_card
    assert msg.share_card.is_wechat_miniprogram


@pytest.mark.parametrize(
    "field,value",
    [
        ("app", "com.tencent.news"),
        ("app", "xcom.tencent.miniapp.lua"),
        ("view", "news"),
        ("meta", []),
        ("meta", {"miniapp": []}),
        ("meta", {}),
    ],
)
def test_unrecognized_miniapp_shape_is_not_structural(field, value):
    card = deepcopy(WECHAT)
    card[field] = value
    msg = OneBotMessageSource().parse_group_message(payload([card]))
    assert not msg.share_card.is_wechat_miniprogram


@pytest.mark.parametrize(
    "tag,icon",
    [
        ("QQ小程序", "https://miniapp.gtimg.cn/public/miniwx.png"),
        ("微信小程序", "https://example.invalid/public/miniwx.png"),
        ("微信小程序", "https://miniapp.gtimg.cn.evil.invalid/public/miniwx.png"),
        ("微信小程序", "https://user@miniapp.gtimg.cn/public/miniwx.png"),
        ("微信小程序", "https://miniapp.gtimg.cn/public/miniwx.png?unknown=1"),
        ("微信小程序", "https://miniapp.gtimg.cn/public/miniqq.png"),
    ],
)
def test_wechat_label_alone_does_not_classify_qq_or_ordinary_cards(tag, icon):
    card = deepcopy(WECHAT)
    card["meta"]["miniapp"].update(tag=tag, tagIcon=icon)
    msg = OneBotMessageSource().parse_group_message(payload([card]))
    assert not msg.share_card.is_wechat_miniprogram


@pytest.mark.parametrize(
    "url",
    [
        "mqqapi://card/show_pslcard?uin=900000003&card_type=qq",
        "mqqapi://card/show_pslcard?uin=900000003&card_type=group&card_type=qq",
        "mqqapi://card/show_pslcard?uin=900000003&uin=900000004&card_type=group",
        "mqqapi://evil.invalid/show_pslcard?uin=900000003&card_type=group",
        "mqqapi://user@card/show_pslcard?uin=900000003&card_type=group",
        "mqqapi://card/show_pslcard?uin=unknown&card_type=group",
        "mqqapi://card/show_pslcard?uin=900000003&card_type=group#other",
    ],
)
def test_contact_card_requires_unambiguous_group_destination(url):
    card = deepcopy(GROUP)
    card["meta"]["contact"]["jumpUrl"] = url
    msg = OneBotMessageSource().parse_group_message(payload([card]))
    assert not msg.share_card.is_group_card


def test_official_adapter_uses_structured_wechat_fields_not_text():
    raw = {
        "id": "synthetic",
        "group_openid": "synthetic-group",
        "author": {"member_openid": "synthetic-member"},
        "content": "[卡片消息] 小程序",
        "ark_data": {
            "ark_type": "miniapp",
            "fields": {
                "tag": "微信小程序",
                "tag_icon": "https://miniapp.gtimg.cn/public/miniwx.png",
                "title": "合成测试",
            },
        },
    }
    assert parse_group_message(raw).share_card.is_wechat_miniprogram
    del raw["ark_data"]
    raw["content"] += "\nsource: 微信小程序"
    assert not parse_group_message(raw).share_card.is_wechat_miniprogram


def message(*, role="member", marked=True, kind="share_card"):
    return StandardMessage(
        message_id="synthetic-" + uuid.uuid4().hex,
        provider="onebot",
        external_group_id="900000001",
        sender=Sender(member_openid="900000002", role=role),
        kind=kind,
        text="校园活动",
        share_card=ShareCardInfo.model_validate(
            {"source": "万能校园墙", "title": "校园活动", "is_wechat_miniprogram": marked}
        ),
    )


@pytest.mark.parametrize("kind", ["share_card", "mixed", "image"])
def test_wechat_card_is_recalled_even_with_allowed_source_or_keyword(kind):
    msg = message(kind=kind)
    decision = TextRuleEngine(allow_terms=frozenset({"校园活动"})).evaluate(msg)
    final = ReviewGate().review(msg, decision)
    assert final.verdict == "violation_high"
    assert RULE in {h.rule_id for h in final.rule_hits}
    assert "recall" in final.recommended_actions
    assert not is_pairing_candidate(msg, final)


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_wechat_card_preserves_protected_roles(role):
    decision = TextRuleEngine().evaluate(message(role=role))
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_wechat_card_preserves_member_allowlist():
    decision = TextRuleEngine(allow_members=frozenset({("onebot", "900000002")})).evaluate(
        message()
    )
    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_ordinary_card_does_not_get_new_structural_rule():
    decision = TextRuleEngine().evaluate(message(marked=False))
    assert RULE not in {h.rule_id for h in decision.rule_hits}
    assert decision.recommended_actions == []


def test_structural_marker_is_hard_evidence_and_excludes_image_window():
    msg = message(kind="image")
    decision = ModerationDecision(
        message_id=msg.message_id,
        verdict="violation_high",
        category="ad",
        confidence=0.95,
        recommended_actions=["recall"],
        rule_hits=[RuleHit(rule_id=RULE, rule_name="wechat_miniprogram_card", category="ad")],
    )
    assert ReviewGate().review(msg, decision).verdict == "violation_high"
    assert not is_pairing_candidate(msg, decision)


@pytest.mark.parametrize("card", [WECHAT, GROUP])
@pytest.mark.parametrize("role", ["member", "owner", "admin"])
async def test_pipeline_keeps_structural_policy_and_role_protection(card, role):
    from app.db import SessionLocal
    from app.runtime.pipeline import run_pipeline

    raw = payload([card], role=role, prefix=[{"type": "text", "data": {"text": "看看"}}])
    raw["group_id"] = "synthetic-" + uuid.uuid4().hex
    async with SessionLocal() as session:
        row = await run_pipeline(raw, session, message_source=OneBotMessageSource())
        assert row is not None
        detail = json.loads(row.detail_json)
        if role == "member":
            assert row.verdict == "violation_high"
            assert "recall" in detail["recommended_actions"]
        else:
            assert row.verdict == "allow"
            assert detail["recommended_actions"] == []
        assert await run_pipeline(raw, session, message_source=OneBotMessageSource()) is None


@pytest.mark.parametrize("mode", ["SHADOW", "OFFICIAL"])
@pytest.mark.parametrize("card", [WECHAT, GROUP])
async def test_card_actions_are_recall_only_and_idempotent(mode, card):
    from app.actions.orchestrator import orchestrate_actions
    from app.config import Settings
    from app.core.routing import upsert_group_route
    from app.db import SessionLocal
    from app.models import ProviderGroupSettings

    from tests.test_onebot_actions import _FakeOneBotClient

    raw = payload([card])
    raw["group_id"] = str(uuid.uuid4().int)[:10]
    msg = OneBotMessageSource().parse_group_message(raw)
    decision = ReviewGate().review(msg, TextRuleEngine().evaluate(msg))
    settings = Settings(
        app_env="prod",
        admin_password="synthetic-test-password",
        qq_app_id="synthetic",
        qq_app_secret="synthetic",
        action_mode=mode,
        onebot_actions_enabled=True,
        onebot_action_stage="recall_only",
        onebot_self_id="10000001",
        emergency_stop=False,
        _env_file=None,
    )
    client = _FakeOneBotClient()
    async with SessionLocal() as session:
        await upsert_group_route(
            session, msg.external_group_id, message_provider="onebot", action_provider="onebot"
        )
        session.add(
            ProviderGroupSettings(
                provider="onebot",
                external_group_id=msg.external_group_id,
                moderation_enabled=True,
                action_enabled=True,
            )
        )
        await session.commit()
        for _ in range(2):
            intents = await orchestrate_actions(
                session, msg, decision, onebot_client=client, settings=settings
            )
            if mode == "SHADOW":
                assert intents == []
            else:
                assert [(i.action, i.status) for i in intents] == [("recall", "SUCCEEDED")]
    assert [c[0] for c in client.calls] == ([] if mode == "SHADOW" else ["recall"])
