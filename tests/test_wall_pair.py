"""校园墙紧邻文字配对豁免测试（负责人 2026-09-14 口径）。"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.moderation.decision import ModerationDecision
from app.moderation.wall_pair import (
    PAIRED_KEY,
    extract_wall_text,
    maybe_wall_text_pairing,
    similarity,
)
from app.runtime.models import ShadowDecision


def _decision(category: str = "ad", verdict: str = "violation_high") -> ModerationDecision:
    return ModerationDecision(
        message_id="m-" + uuid.uuid4().hex,
        external_group_id="G_PAIR",
        verdict=verdict,
        category=category,
        confidence=0.95,
        reason="广告",
        recommended_actions=["recall", "mute", "warn"],
    )


def _new_group() -> str:
    """每个测试独立群，避免互相串扰（窗口内历史图会影响断言）。"""
    return "G-" + uuid.uuid4().hex[:12]


def _msg(text: str, kind: str = "text", group: str = "G_PAIR"):
    """构造 StandardMessage 最小桩（只用到豁免函数读取的字段）。"""

    class _S:
        username = "tester"

    class _M:
        pass

    _M.provider = "onebot"
    _M.external_group_id = group
    _M.external_user_id = "U-" + group
    _M.message_id = "msg-" + uuid.uuid4().hex
    _M.external_message_id = "em-" + uuid.uuid4().hex
    _M.kind = kind
    _M.text = text
    _M.sender = _S()
    return _M()


def _wall_detail(text: str, paired: bool = False) -> str:
    return json.dumps(
        {
            "ai_results": [
                {
                    "source": "vision",
                    "evidence": f"校园墙白名单|文案:{text}",
                }
            ],
            PAIRED_KEY: paired,
        },
        ensure_ascii=False,
    )


async def _insert_wall_image(
    *, minutes_ago: float = 0.5, wall_text: str = "", paired: bool = False, group: str = "G_PAIR"
) -> ShadowDecision:
    async with SessionLocal() as session:
        row = ShadowDecision(
            message_id="sd-" + uuid.uuid4().hex,
            provider="onebot",
            group_openid=group,
            member_openid="U-" + group,
            external_group_id=group,
            external_user_id="U-" + group,
            kind="image",
            verdict="allow",
            category="",
            confidence=1.0,
            reason="校园墙白名单",
            detail_json=_wall_detail(wall_text, paired),
        )
        session.add(row)
        await session.commit()
        if minutes_ago:
            created = (datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)).replace(microsecond=0)
            await session.execute(
                ShadowDecision.__table__.update()
                .where(ShadowDecision.message_id == row.message_id)
                .values(created_at=created)
            )
            await session.commit()
        return row


def test_similarity_threshold() -> None:
    promo = "招兼职：看抖音漫剧，多劳多得，有意者联系1878969877王经理"
    assert similarity(promo, promo + "！！") >= 0.60
    assert similarity(promo, "今天天气不错适合出去走走看看风景散散心") < 0.60
    assert similarity("", promo) == 0.0


def test_extract_wall_text_formats() -> None:
    assert extract_wall_text("校园墙白名单|文案:招兼职") == "招兼职"
    assert extract_wall_text("校园墙白名单来源") == ""
    assert extract_wall_text("图片为广告") is None
    assert extract_wall_text("") is None


@pytest.mark.asyncio
async def test_pairing_exempts_adjacent_text() -> None:
    """口径主路径：2 分钟内的校园墙图 + 相似文字 → 不撤回、消耗配对名额。"""
    g = _new_group()
    wall_text = "招兼职：看抖音漫剧，多劳多得，有梦想你就来，有意者联系1878969877王经理"
    row = await _insert_wall_image(wall_text=wall_text, group=g)
    decision = await _run_pairing(wall_text + "！！", group=g)
    assert decision.verdict == "record_only"
    assert decision.recommended_actions == []
    assert "豁免" in decision.reason
    async with SessionLocal() as session:
        fresh = (
            await session.execute(
                select(ShadowDecision).where(ShadowDecision.message_id == row.message_id)
            )
        ).scalar_one()
        assert json.loads(fresh.detail_json)[PAIRED_KEY] is True


async def _run_pairing(text: str, group: str = "G_PAIR") -> ModerationDecision:
    async with SessionLocal() as session:
        result = await maybe_wall_text_pairing(session, _msg(text, group=group), _decision())
        await session.commit()
        return result


@pytest.mark.asyncio
async def test_pairing_consumed_once() -> None:
    """每张图只豁免一条：第二条相似文字不再豁免。"""
    g = _new_group()
    wall_text = "招兼职：抖音漫剧合作，有意者联系1878969877王经理，多劳多得"
    await _insert_wall_image(wall_text=wall_text, group=g)
    first = await _run_pairing(wall_text + "！", group=g)
    assert first.verdict == "record_only"
    second = await _run_pairing(wall_text + "！", group=g)
    assert second.verdict == "violation_high"
    assert second.recommended_actions == ["recall", "mute", "warn"]


@pytest.mark.asyncio
async def test_pairing_window_expires() -> None:
    """超过 2 分钟窗口的图不豁免。"""
    g = _new_group()
    wall_text = "招兼职：看抖音漫剧，多劳多得，有意者联系1878969877王经理"
    await _insert_wall_image(wall_text=wall_text, minutes_ago=3, group=g)
    decision = await _run_pairing(wall_text, group=g)
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_pairing_low_similarity_not_exempt() -> None:
    """相似度不足 60% 不豁免。"""
    g = _new_group()
    await _insert_wall_image(wall_text="校园墙今天发布了三条新帖子欢迎大家围观讨论", group=g)
    decision = await _run_pairing("招兼职：抖音漫剧合作，联系1878969877王经理，多劳多得", group=g)
    assert decision.verdict == "violation_high"


@pytest.mark.asyncio
async def test_pairing_only_for_ad_category() -> None:
    """诈骗/色情等不享受豁免。"""
    g = _new_group()
    wall_text = "招兼职：看抖音漫剧，联系1878969877王经理"
    await _insert_wall_image(wall_text=wall_text, group=g)
    async with SessionLocal() as session:
        result = await maybe_wall_text_pairing(
            session, _msg(wall_text, group=g), _decision(category="fraud")
        )
        await session.commit()
    assert result.verdict == "violation_high"


@pytest.mark.asyncio
async def test_pairing_ignores_non_wall_images() -> None:
    """普通放行图（非校园墙）不构成豁免来源。"""
    g = _new_group()
    async with SessionLocal() as session:
        session.add(
            ShadowDecision(
                message_id="sd-" + uuid.uuid4().hex,
                provider="onebot",
                group_openid=g,
                member_openid="U-" + g,
                external_group_id=g,
                external_user_id="U-" + g,
                kind="image",
                verdict="allow",
                category="",
                confidence=0.9,
                reason="正常图片",
                detail_json=json.dumps(
                    {"ai_results": [{"source": "vision", "evidence": "招兼职：看抖音漫剧，联系王经理"}]}
                ),
            )
        )
        await session.commit()
    decision = await _run_pairing("招兼职：看抖音漫剧，联系1878969877王经理", group=g)
    assert decision.verdict == "violation_high"
