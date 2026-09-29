"""T-205增强：人工反馈喂给视觉AI的测试。"""

from __future__ import annotations

import json
import uuid

import pytest
from app.db import SessionLocal
from app.moderation.ai import PROMPT_VERSION, AIModerationRequest
from app.moderation.feedback import (
    FeedbackRecord,
    load_vision_feedback_context,
)
from app.runtime.models import ShadowDecision


async def _add_image_feedback(
    session,
    *,
    label: str,
    reason: str,
    ai_cat: str | None,
    ai_conf: float,
) -> None:
    mid = f"FB_IMG_{uuid.uuid4().hex[:6]}"
    detail = json.dumps(
        {
            "ai_results": [{"category": ai_cat, "confidence": ai_conf}] if ai_cat else [],
        },
        ensure_ascii=False,
    )
    session.add(
        ShadowDecision(
            message_id=mid,
            external_message_id=mid,
            group_openid="G_FB",
            member_openid="M_FB",
            provider="onebot",
            external_group_id="G_FB",
            external_user_id="M_FB",
            kind="image",
            verdict="record_only",
            detail_json=detail,
        )
    )
    session.add(
        FeedbackRecord(
            message_id=mid,
            group_openid="G_FB",
            member_openid="M_FB",
            provider="onebot",
            external_group_id="G_FB",
            external_user_id="M_FB",
            label=label,
            reason=reason,
            operator="test",
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_single_normal_label_does_not_generalize_into_vision_prompt() -> None:
    """A human label is truth about one sample, not a general allow rule."""
    async with SessionLocal() as session:
        await _add_image_feedback(
            session,
            label="confirmed_normal",
            reason="白名单来源（校园墙等）",
            ai_cat="ad",
            ai_conf=0.9,
        )
        ctx = await load_vision_feedback_context(
            session, provider="onebot", external_group_id="G_FB"
        )
    assert ctx == ""


@pytest.mark.asyncio
async def test_single_violation_label_does_not_generalize_into_vision_prompt() -> None:
    async with SessionLocal() as session:
        await _add_image_feedback(
            session,
            label="confirmed_violation",
            reason="广告/引流",
            ai_cat="other",
            ai_conf=0.9,
        )
        ctx = await load_vision_feedback_context(
            session, provider="onebot", external_group_id="G_FB"
        )
    assert ctx == ""


@pytest.mark.asyncio
async def test_matching_human_label_stays_out_of_online_prompt() -> None:
    async with SessionLocal() as session:
        await _add_image_feedback(
            session,
            label="confirmed_violation",
            reason="广告/引流",
            ai_cat="ad",
            ai_conf=0.95,
        )
        ctx = await load_vision_feedback_context(
            session, provider="onebot", external_group_id="G_FB"
        )
    assert ctx == ""


@pytest.mark.asyncio
async def test_empty_feedback_context_when_no_records() -> None:
    """无反馈记录时返回空字符串。"""
    async with SessionLocal() as session:
        ctx = await load_vision_feedback_context(session)
    # 测试DB可能有其他测试的残留，只要不含我们测试群的纠正即可
    # 这里只验证不报错且是字符串
    assert ctx == ""


def test_prompt_version_bumped_for_feedback() -> None:
    """反馈上下文注入后 PROMPT_VERSION 必须升级（缓存键含版本号）。

    只校验"版本不低于引入反馈上下文的 v6"，不钉死具体值——提示词/契约每次
    变更都应继续升版（例如 D-039 小程序码放行升到 t204-v14）。
    """
    assert PROMPT_VERSION.startswith("t204-v")
    assert int(PROMPT_VERSION.removeprefix("t204-v")) >= 6


def test_request_carries_feedback_context() -> None:
    """AIModerationRequest 支持 feedback_context 字段。"""
    req = AIModerationRequest(
        message_id="T_FB",
        group_openid="G",
        content_kind="image",
        feedback_context="人工纠正：你之前判ad但人工确认正常→应判null",
    )
    assert req.feedback_context != ""


@pytest.mark.asyncio
async def test_cache_key_includes_feedback_context() -> None:
    """不同 feedback_context 产生不同缓存键。"""
    from app.moderation.ai import ai_cache_key

    req1 = AIModerationRequest(
        message_id="T1", group_openid="G", content_kind="image", feedback_context="A"
    )
    req2 = AIModerationRequest(
        message_id="T2", group_openid="G", content_kind="image", feedback_context="B"
    )
    key1 = ai_cache_key(req1, model_id="m", prompt_version="v3")
    key2 = ai_cache_key(req2, model_id="m", prompt_version="v3")
    assert key1 != key2
