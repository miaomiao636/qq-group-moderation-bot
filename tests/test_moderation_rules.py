"""T-103 规则引擎测试：正反例离线评测 + 决策结构安全约束（永无kick）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.adapters.qq_official.contract import Attachment, Sender, ShareCardInfo, StandardMessage
from app.adapters.qq_official.parser import parse_group_message
from app.moderation.decision import (
    FORWARD_RECORD_RECALL_RULE_ID,
    GROUP_CARD_RECALL_RULE_ID,
)
from app.moderation.rules import TextRuleEngine

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "qq_official"


def make_message(
    text: str = "",
    *,
    role: str = "member",
    member_openid: str = "MEM_TEST",
    attachments: list[Attachment] | None = None,
    message_id: str = "MSG_TEST_0001",
    kind: str = "text",
    share_card: ShareCardInfo | None = None,
) -> StandardMessage:
    return StandardMessage(
        message_id=message_id,
        group_openid="GROUP_TEST",
        sender=Sender(member_openid=member_openid, role=role),
        kind=kind,
        text=text,
        attachments=attachments or [],
        share_card=share_card,
    )


def fixture_message(name: str) -> StandardMessage:
    payload: dict[str, Any] = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))["data"]
    return parse_group_message(payload)


# ---------- 决策结构安全约束 ----------


def test_decision_structure_never_contains_kick() -> None:
    """任何决策的 recommended_actions 都不允许出现 kick（D-001 硬约束）。"""
    allowed = {"recall", "mute", "warn", ""}
    spam = make_message(text="招募兼职刷单，日结，加我微信 abc12345")
    engine = TextRuleEngine()
    decision = engine.evaluate(spam)
    assert all(action in allowed for action in decision.recommended_actions)
    # 全量字段校验：序列化后也不得出现 kick 字样
    dumped = decision.model_dump_json()
    assert "kick" not in dumped.lower()


def test_recommended_actions_only_on_high_verdict() -> None:
    engine = TextRuleEngine()
    medium = engine.evaluate(make_message(text="今天天气不错"))
    weak = engine.evaluate(make_message(text="有人一起打游戏吗"))
    for decision in (medium, weak):
        if decision.verdict != "violation_high":
            assert decision.recommended_actions == []


# ---------- 违规正例（来自负责人实测样本） ----------


@pytest.mark.parametrize(
    "text",
    [
        "居家简易线上任务，无需押金垫资，日收益80-200，多劳多得，限安卓手机，裙号1097056029",
        "招：小红薯评论员，不代发安全可靠，一单10秒结，操作简单可试做",
        "群里到30就开始干活，评论一条3/5块，评论完截图，支付宝口令红包结算",
        "企业微红标80/天，绿标500/天，日租当天结，感兴趣的加我",
        "招素人/团长，抖y发文，店铺推广单一条7-10米不等，日结，有兴趣联系",
        "在吗你看看这个，我感觉挺适合你的！朋友在卖兰州市中心5.0版农贸市场的商铺呢（摊位、档口）总价5万即可买一套，一年可收租金6000到10000多，而且还有房产证",
        "【家长直招｜龙湾锦园家教】温十五中初二男生，全科补习，基础薄弱，找大一在校学生上门。工作日晚上1小时，盯作业、订正题，中介勿扰，学生本人联系",
    ],
)
def test_spam_texts_flagged(text: str) -> None:
    decision = TextRuleEngine().evaluate(make_message(text=text))
    assert decision.verdict == "violation_high", (
        f"应为高置信违规: {decision.reason} hits={[(h.rule_id, h.confidence_delta) for h in decision.rule_hits]}"
    )
    assert decision.category in ("ad", "fraud")
    assert set(decision.recommended_actions) == {"recall", "mute", "warn"}


# ---------- 允许反例 ----------


@pytest.mark.parametrize(
    "text",
    [
        "今天食堂的番茄炒蛋真好吃",
        "有人一起去图书馆吗",
        "明天考试，大家加油",
        "这个游戏新版本更新了，你们玩了吗",
        "周末打球去不去",
    ],
)
def test_normal_texts_allowed(text: str) -> None:
    decision = TextRuleEngine().evaluate(make_message(text=text))
    assert decision.verdict == "allow", f"正常聊天不应命中: {decision.reason}"


@pytest.mark.parametrize(
    "text",
    [
        "大家觉得兼职靠谱吗？我怕被骗",
        "学校让我们讨论刷单骗局，请大家不要上当",
        "我不做兼职，谢谢",
    ],
)
def test_discussion_and_negation_are_not_high_risk(text: str) -> None:
    decision = TextRuleEngine().evaluate(make_message(text=text))
    assert decision.verdict != "violation_high"
    assert decision.recommended_actions == []


def test_allowed_share_source_is_allowed() -> None:
    msg = make_message(
        text="[卡片消息] 小程序\nsource: 万能校园墙\ntitle: 校园墙信息",
        kind="share_card",
        share_card=ShareCardInfo(source="万能校园墙", title="校园墙信息"),
    )

    decision = TextRuleEngine().evaluate(msg)

    assert decision.verdict == "allow"
    assert decision.recommended_actions == []


def test_unknown_share_card_is_record_only_without_ad_evidence() -> None:
    msg = make_message(
        text="[卡片消息] 小程序\nsource: 未知小程序\ntitle: 普通分享",
        kind="share_card",
        share_card=ShareCardInfo(source="未知小程序", title="普通分享"),
    )

    decision = TextRuleEngine().evaluate(msg)

    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


# ---------- 保护角色 ----------


def test_owner_and_admin_never_punished() -> None:
    engine = TextRuleEngine()
    for role in ("owner", "admin"):
        decision = engine.evaluate(
            make_message(text="招募兼职刷单，日结，加我微信 abc12345", role=role)
        )
        assert decision.verdict == "record_only"
        assert decision.is_protected_sender is True
        assert decision.recommended_actions == []


# ---------- 刷屏 ----------


def test_flood_same_content_3_in_60s() -> None:
    engine = TextRuleEngine()
    base = 1000.0
    verdict = "allow"
    for i in range(3):
        decision = engine.evaluate(
            make_message(text="水一水", member_openid="MEM_FLOOD", message_id=f"FLOOD_{i}"),
            now=base + i * 5,  # 每5秒一条，共3条在60秒窗口内
        )
        verdict = decision.verdict
    assert verdict == "violation_high"


def test_no_flood_when_spread_out() -> None:
    engine = TextRuleEngine()
    base = 2000.0
    verdict = "allow"
    for i in range(8):
        decision = engine.evaluate(
            make_message(text="水一水", member_openid="MEM_SPREAD", message_id=f"SPREAD_{i}"),
            now=base + i * 35,  # 每35秒一条，60秒窗口内最多2条
        )
        verdict = decision.verdict
    assert verdict == "allow"


def test_flood_different_content_not_counted() -> None:
    engine = TextRuleEngine()
    base = 3000.0
    verdict = "allow"
    for i in range(8):
        decision = engine.evaluate(
            make_message(
                text=f"不同的内容编号{i}", member_openid="MEM_DIFF", message_id=f"DIFF_{i}"
            ),
            now=base + i * 2,
        )
        verdict = decision.verdict
    assert verdict == "allow"


# ---------- 真实样本fixture评测 ----------


def test_spam_fixture_text_flagged() -> None:
    msg = fixture_message("group_message_create_text_spam_1.json")
    if not msg.text.strip() or msg.kind != "text":
        pytest.skip("非文字样本")
    decision = TextRuleEngine().evaluate(msg)
    assert decision.verdict in ("violation_high", "record_only")


def test_forward_and_card_not_misjudged_by_text_engine() -> None:
    """转发记录/卡片骨架的判定（2026-09-18 负责人口径更新）。

    - **合并转发**：负责人 2026-09-18 明确"一律撤回"，由本地确定性规则
      `R_FORWARD_RECORD` 给出高置信 + recall（无需展开内容）；群主/管理员与成员
      白名单成员除外。
    - **分享卡片**：文本引擎不得仅凭卡片骨架判高置信（多模态证据由 T-201/T-202
      处理）；"群名片"（is_group_card）另行由 `R_GROUP_CARD` 一律撤回。
    """
    engine = TextRuleEngine()
    forward = engine.evaluate(fixture_message("group_message_create_forward_record.json"))
    assert forward.verdict == "violation_high"
    assert forward.recommended_actions[0] == "recall"
    assert any(h.rule_id == FORWARD_RECORD_RECALL_RULE_ID for h in forward.rule_hits)

    card = engine.evaluate(fixture_message("group_message_create_share_card.json"))
    assert card.verdict != "violation_high" or card.category in ("ad", "fraud")
    assert not any(h.rule_id == GROUP_CARD_RECALL_RULE_ID for h in card.rule_hits)


# ---------- 离线评测报告 ----------


def test_offline_evaluation_report() -> None:
    """正例必须命中、反例必须放行；输出可复现的评测摘要。"""
    positives = [
        "居家简易线上任务，无需押金垫资，日收益80-200，裙号1097056029",
        "招募抖音线上兼职，简单开播挂机，后台保持在线即可",
        "小红书评论员，一单10秒结，支付宝口令红包结算",
    ]
    negatives = [
        "今天天气不错",
        "明天下午一起自习吗",
        "食堂新开的窗口还不错",
    ]
    engine = TextRuleEngine()
    tp = sum(
        1 for t in positives if engine.evaluate(make_message(text=t)).verdict == "violation_high"
    )
    fp = sum(1 for t in negatives if engine.evaluate(make_message(text=t)).verdict != "allow")
    assert tp == len(positives), "所有违规正例必须高置信命中"
    assert fp == 0, "所有正常反例必须放行"
