"""验收统计不能把未标注当正常，也不能重复或跨身份匹配反馈。"""

from __future__ import annotations

import pytest
from app.db import Base
from app.moderation.feedback import FeedbackRecord
from app.reports.stats import _agreement
from app.runtime.models import ShadowDecision
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.mark.asyncio
async def test_only_latest_explicit_human_labels_are_truth() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            for mid, verdict in [
                ("unknown", "allow"),
                ("changed", "violation_high"),
                ("wrong-group", "allow"),
            ]:
                session.add(
                    ShadowDecision(
                        message_id=mid,
                        provider="onebot",
                        external_group_id="g",
                        group_openid="g",
                        member_openid="u",
                        verdict=verdict,
                    )
                )
            for mid, label, group in [
                ("unknown", "unknown_recall", "g"),
                ("changed", "confirmed_violation", "g"),
                ("changed", "false_positive", "g"),
                ("wrong-group", "confirmed_normal", "another"),
            ]:
                session.add(
                    FeedbackRecord(
                        message_id=mid,
                        provider="onebot",
                        external_group_id=group,
                        group_openid=group,
                        label=label,
                        operator="test-admin",
                    )
                )
            await session.commit()
            result = await _agreement(session)
            assert result["total"] == 1
            assert result["agree"] == 0
            assert result["false_positive"] == 1
            assert result["precision"] == 0.0
            assert result["recall"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_no_labels_is_unavailable_not_zero_accuracy() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            result = await _agreement(session)
            assert result["total"] == 0
            assert result["precision"] is None
            assert result["recall"] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_counts_actual_calls_separately_from_cache_and_blocked() -> None:
    from app.moderation.ai import AIUsageLog
    from app.reports.stats import build_stats

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            for index, (source, error, ok) in enumerate(
                [
                    ("vision_primary", "", True),
                    ("vision_secondary", "", True),
                    ("cache_primary", "", True),
                    ("error_primary", "ai_rate_or_budget_limited", False),
                    ("error_secondary", "provider_call_failed", False),
                ]
            ):
                session.add(
                    AIUsageLog(
                        provider="test",
                        model_id="test-model",
                        group_openid="g",
                        message_id=f"stats-{index}",
                        source=source,
                        error_kind=error,
                        ok=ok,
                        cost_cents=0,
                    )
                )
            await session.commit()
            result = await build_stats(session)
            assert result["totals"]["ai_calls"] == 3
            assert result["totals"]["ai_failed"] == 1
            assert result["ai_usage"]["primary_calls"] == 1
            assert result["ai_usage"]["secondary_calls"] == 2
            assert result["ai_usage"]["cache_hits"] == 1
            assert result["ai_usage"]["blocked_calls"] == 1
            assert result["ai_by_model"][0]["calls"] == 3
            assert result["ai_by_model"][0]["cost_known"] is False
            from app.web.routes import _ai_daily_table, _build_ai_daily_stats, _stats_body

            daily = await _build_ai_daily_stats(session)
            assert len(daily) == 1
            assert daily[0]["calls"] == 3
            assert daily[0]["secondary_calls"] == 2
            assert daily[0]["cache_hits"] == 1
            assert daily[0]["blocked_calls"] == 1
            assert daily[0]["cost_yuan"] is None
            assert "未核算" in _ai_daily_table(daily)
            assert "次模型" in _stats_body(result)
    finally:
        await engine.dispose()
