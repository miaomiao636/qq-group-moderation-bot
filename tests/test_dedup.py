"""T-102/R-102 幂等去重测试（新语义：成功才标记 PROCESSED，失败可重试）。"""

from __future__ import annotations

import pytest
from app.adapters.qq_official.dedup import (
    begin_processing,
    mark_failed,
    mark_processed,
    reset_memory_cache,
)
from app.db import SessionLocal


@pytest.fixture(autouse=True)
def _clean_memory() -> None:
    reset_memory_cache()
    yield
    reset_memory_cache()


@pytest.mark.asyncio
async def test_first_seen_returns_true_then_duplicates_false_after_success() -> None:
    mid = "TEST_MSG_DUP_1"
    async with SessionLocal() as session:
        claim = await begin_processing(session, mid)
        assert claim.accepted is True
        await mark_processed(session, mid, claim.token)
        assert (await begin_processing(session, mid)).accepted is False  # 成功后重复推送跳过
        assert (await begin_processing(session, mid)).accepted is False


@pytest.mark.asyncio
async def test_failed_event_is_retryable() -> None:
    """R-102-2：处理失败标记 FAILED，下次推送必须可重试。"""
    mid = "TEST_MSG_RETRY_1"
    async with SessionLocal() as session:
        claim = await begin_processing(session, mid)
        assert claim.accepted is True
        await mark_failed(session, mid, claim.token, "解析失败: 数据损坏")
        # 失败后重试：允许继续处理
        retry = await begin_processing(session, mid)
        assert retry.accepted is True
        await mark_processed(session, mid, retry.token)
        assert (await begin_processing(session, mid)).accepted is False


@pytest.mark.asyncio
async def test_persists_across_sessions() -> None:
    mid = "TEST_MSG_PERSIST"
    async with SessionLocal() as session:
        claim = await begin_processing(session, mid)
        assert claim.accepted is True
        await mark_processed(session, mid, claim.token)
    reset_memory_cache()
    async with SessionLocal() as session:
        assert (await begin_processing(session, mid)).accepted is False


@pytest.mark.asyncio
async def test_different_ids_independent() -> None:
    async with SessionLocal() as session:
        a = await begin_processing(session, "TEST_MSG_A")
        b = await begin_processing(session, "TEST_MSG_B")
        assert a.accepted is True
        assert b.accepted is True
        await mark_processed(session, "TEST_MSG_A", a.token)
        await mark_processed(session, "TEST_MSG_B", b.token)


@pytest.mark.asyncio
async def test_processing_state_requires_lease_expiry() -> None:
    """PROCESSING 未过期不可重领；过期后可接管，旧令牌不能完成新任务。"""
    mid = "TEST_MSG_PROC"
    async with SessionLocal() as session:
        first = await begin_processing(session, mid, lease_seconds=-1)
        assert first.accepted is True
        second = await begin_processing(session, mid)
        assert second.accepted is True
        assert second.token != first.token
        assert await mark_processed(session, mid, first.token) is False
        assert await mark_processed(session, mid, second.token) is True
        assert (await begin_processing(session, mid)).accepted is False
