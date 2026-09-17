"""校园墙图后广告文字豁免测试（负责人 2026-09-17 口径 C；R06/R09 加固保留）。

口径 C：图后 2 分钟内该成员的任意广告文字豁免（不再要求相似度/紧邻/一图一条）；
fraud 等非广告不豁免；来源图必须为视觉结构化确认的校园墙图（含图内文案）。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db import SessionLocal
from app.moderation.decision import ModerationDecision
from app.moderation.wall_pair import (
    extract_wall_text,
    maybe_wall_text_pairing,
)
from app.runtime.models import ShadowDecision

WALL_TEXT = "招兼职：看抖音漫剧，多劳多得，有梦想你就来，有意者联系1878969877王经理"


def _new_group() -> str:
    """每个测试独立群，避免窗口内历史图互相串扰。"""
    return "G-" + uuid.uuid4().hex[:12]


def _decision(category: str = "ad", message_id: str | None = None) -> ModerationDecision:
    return ModerationDecision(
        message_id=message_id or ("m-" + uuid.uuid4().hex),
        external_group_id="G_PAIR",
        verdict="violation_high",
        category=category,
        confidence=0.95,
        reason="广告",
        recommended_actions=["recall", "mute", "warn"],
    )


def _msg(text: str, group: str = "G_PAIR", sent_at: datetime | None = None):
    """StandardMessage 最小桩（豁免函数只读取这些字段）。"""

    class _S:
        username = "tester"

    class _M:
        pass

    _M.provider = "onebot"
    _M.external_group_id = group
    _M.external_user_id = "U-" + group
    _M.message_id = "msg-" + uuid.uuid4().hex
    _M.external_message_id = "em-" + uuid.uuid4().hex
    _M.kind = "text"
    _M.text = text
    _M.sender = _S()
    _M.sent_at = sent_at
    return _M()


def _wall_detail(
    wall_text: str = "",
    *,
    sent_at: str = "",
    vision_evidence: str | None = None,
    vision_cat: str | None = None,
    vision_nr: bool = False,
) -> str:
    return json.dumps(
        {
            "sent_at": sent_at,
            "ai_results": [
                {
                    "source": "vision",
                    "evidence": vision_evidence or f"校园墙白名单|文案:{wall_text}",
                    "category": vision_cat,
                    "needs_review": vision_nr,
                    "degraded_reason": "",
                }
            ],
        },
        ensure_ascii=False,
    )


async def _insert_wall_image(
    *,
    group: str,
    wall_text: str = "",
    seconds_ago: float = 30,
    detail: str | None = None,
    vision_evidence: str | None = None,
    vision_cat: str | None = None,
    vision_nr: bool = False,
    verdict: str = "allow",
) -> ShadowDecision:
    """插入一条校园墙图判定：detail.sent_at = now - seconds_ago。"""
    sent_iso = (
        (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
        if seconds_ago is not None
        else ""
    )
    if detail is None:
        detail = _wall_detail(
            wall_text,
            sent_at=sent_iso,
            vision_evidence=vision_evidence,
            vision_cat=vision_cat,
            vision_nr=vision_nr,
        )
    async with SessionLocal() as session:
        row = ShadowDecision(
            message_id="sd-" + uuid.uuid4().hex,
            provider="onebot",
            group_openid=group,
            member_openid="U-" + group,
            external_group_id=group,
            external_user_id="U-" + group,
            kind="image",
            verdict=verdict,
            category="",
            confidence=1.0,
            reason="校园墙白名单",
            detail_json=detail,
        )
        session.add(row)
        await session.commit()
        return row


async def _insert_text_row(group: str, seconds_ago: float) -> None:
    """插入该成员的一条历史消息（口径 C：不再阻断豁免，仅验证不干扰）。"""
    async with SessionLocal() as session:
        session.add(
            ShadowDecision(
                message_id="sd-" + uuid.uuid4().hex,
                provider="onebot",
                group_openid=group,
                member_openid="U-" + group,
                external_group_id=group,
                external_user_id="U-" + group,
                kind="text",
                verdict="record_only",
                category="",
                confidence=0.9,
                reason="普通消息",
                detail_json=json.dumps(
                    {"sent_at": (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()}
                ),
            )
        )
        await session.commit()


async def _run_pairing(
    text: str, group: str, decision: ModerationDecision | None = None
) -> ModerationDecision:
    msg_sent = datetime.now(UTC)
    async with SessionLocal() as session:
        result = await maybe_wall_text_pairing(
            session, _msg(text, group=group, sent_at=msg_sent), decision or _decision()
        )
        await session.commit()
        return result


def test_extract_wall_text_prefix_only() -> None:
    """R09: 仅前缀匹配构成许可，否定表述与子串不构成。"""
    assert extract_wall_text("校园墙白名单|文案:招兼职") == "招兼职"
    assert extract_wall_text("校园墙白名单来源") is None
    assert extract_wall_text("不符合校园墙白名单|文案:招兼职") is None
    assert extract_wall_text("图片为广告：校园墙白名单|文案:x") is None
    assert extract_wall_text("") is None


@pytest.mark.asyncio
async def test_wall_window_exempts_text_without_similarity() -> None:
    """口径 C 主路径：图后 2 分钟内任意广告文字豁免——改写文案（低相似度）同样豁免。"""
    g = _new_group()
    await _insert_wall_image(group=g, wall_text=WALL_TEXT)
    rewrite = "未注册注销过大麦的手机号来 立帆4 两天后在返3"
    decision = await _run_pairing(rewrite, g)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert "豁免" in decision.reason


@pytest.mark.asyncio
async def test_retry_recomputes_exemption() -> None:
    """无状态重算：同事件重试结果一致（无需绑定，也不消耗新图）。"""
    g = _new_group()
    await _insert_wall_image(group=g, wall_text=WALL_TEXT)
    mid = "m-" + uuid.uuid4().hex
    first = await _run_pairing(WALL_TEXT, g, _decision(message_id=mid))
    assert first.verdict == "record_only"
    second = await _run_pairing(WALL_TEXT, g, _decision(message_id=mid))
    assert second.verdict == "record_only"
    assert second.recommended_actions == []


@pytest.mark.asyncio
async def test_one_image_exempts_multiple_texts_within_window() -> None:
    """口径 C：不再「一图一条」——窗口内多条文字共享同一张确认图。"""
    g = _new_group()
    await _insert_wall_image(group=g, wall_text=WALL_TEXT)
    first = await _run_pairing(WALL_TEXT + "！", g, _decision())
    assert first.verdict == "record_only"
    second = await _run_pairing("另一条改写文案：联系王经理看漫剧", g, _decision())
    assert second.verdict == "record_only"
    assert second.recommended_actions == []


@pytest.mark.asyncio
async def test_record_only_wall_image_still_a_source() -> None:
    """来源图判定为 record_only（如 2026-09-17 实案：媒体层保守转人工）仍构成豁免来源。"""
    g = _new_group()
    await _insert_wall_image(group=g, wall_text=WALL_TEXT, verdict="record_only")
    decision = await _run_pairing("改写文案：大麦手机号来 立帆4 两天后在返3", g)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []


@pytest.mark.asyncio
async def test_r06_window_on_sent_time_expires() -> None:
    """R06: 按消息发送时间计窗口——图早于 2 分钟不豁免。"""
    g = _new_group()
    await _insert_wall_image(group=g, wall_text=WALL_TEXT, seconds_ago=181)
    decision = await _run_pairing(WALL_TEXT, g)
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_window_boundary_on_sent_time() -> None:
    """窗口边界：119 秒内豁免；121 秒外不豁免。"""
    g1 = _new_group()
    await _insert_wall_image(group=g1, wall_text=WALL_TEXT, seconds_ago=119)
    assert (await _run_pairing(WALL_TEXT, g1)).verdict == "record_only"
    g2 = _new_group()
    await _insert_wall_image(group=g2, wall_text=WALL_TEXT, seconds_ago=121)
    assert (await _run_pairing(WALL_TEXT, g2)).verdict == "violation_high"


@pytest.mark.asyncio
async def test_r06_missing_sent_at_conservative() -> None:
    """R06: 图 detail 缺 sent_at（旧格式）保守不豁免。"""
    g = _new_group()
    await _insert_wall_image(
        group=g,
        wall_text=WALL_TEXT,
        seconds_ago=30,
        detail=_wall_detail(WALL_TEXT, sent_at=""),  # 无 sent_at
    )
    decision = await _run_pairing(WALL_TEXT, g)
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_pairing_only_for_ad_category() -> None:
    """诈骗不享受豁免。"""
    g = _new_group()
    await _insert_wall_image(group=g, wall_text=WALL_TEXT)
    async with SessionLocal() as session:
        result = await maybe_wall_text_pairing(
            session,
            _msg(WALL_TEXT, group=g, sent_at=datetime.now(UTC)),
            _decision(category="fraud"),
        )
        await session.commit()
    assert result.verdict == "violation_high"


@pytest.mark.asyncio
async def test_pairing_ignores_non_wall_images() -> None:
    """普通放行图（非校园墙）不构成豁免来源。"""
    g = _new_group()
    await _insert_wall_image(
        group=g,
        wall_text="",
        vision_evidence="招兼职：看抖音漫剧，联系王经理",
    )
    decision = await _run_pairing("招兼职：看抖音漫剧，联系1878969877王经理", g)
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_caption_less_wall_confirmation_not_a_source() -> None:
    """无图内文案的校园墙确认不构成豁免来源（无法确认图文配套关系）。"""
    g = _new_group()
    await _insert_wall_image(group=g, vision_evidence="校园墙白名单")
    decision = await _run_pairing(WALL_TEXT, g)
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_r09_denial_evidence_not_source() -> None:
    """R09: 否定表述（「不符合校园墙白名单|文案:…」）不构成豁免来源。"""
    g = _new_group()
    await _insert_wall_image(
        group=g,
        vision_evidence=f"不符合校园墙白名单|文案:{WALL_TEXT}",
    )
    decision = await _run_pairing(WALL_TEXT, g)
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_r09_uncertain_vision_not_source() -> None:
    """R09: 未消疑/降级/带类别的 vision 结果不构成豁免来源。"""
    g = _new_group()
    await _insert_wall_image(
        group=g,
        vision_evidence=f"校园墙白名单|文案:{WALL_TEXT}",
        vision_cat="ad",
        vision_nr=True,
        verdict="record_only",
    )
    decision = await _run_pairing(WALL_TEXT, g)
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_r09_intervening_message_does_not_block() -> None:
    """口径 C：中间消息不再阻断（紧邻要求取消）。"""
    g = _new_group()
    await _insert_wall_image(group=g, wall_text=WALL_TEXT, seconds_ago=60)
    await _insert_text_row(g, seconds_ago=30)
    decision = await _run_pairing(WALL_TEXT, g)
    assert decision.verdict == "record_only"
