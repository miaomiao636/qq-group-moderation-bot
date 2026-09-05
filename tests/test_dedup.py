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
        assert await begin_processing(session, mid) is True
        await mark_processed(session, mid)
        assert await begin_processing(session, mid) is False  # 成功后重复推送跳过
        assert await begin_processing(session, mid) is False


@pytest.mark.asyncio
async def test_failed_event_is_retryable() -> None:
    """R-102-2：处理失败标记 FAILED，下次推送必须可重试。"""
    mid = "TEST_MSG_RETRY_1"
    async with SessionLocal() as session:
        assert await begin_processing(session, mid) is True
        await mark_failed(session, mid, "解析失败: 数据损坏")
        # 失败后重试：允许继续处理
        assert await begin_processing(session, mid) is True
        await mark_processed(session, mid)
        assert await begin_processing(session, mid) is False


@pytest.mark.asyncio
async def test_persists_across_sessions() -> None:
    mid = "TEST_MSG_PERSIST"
    async with SessionLocal() as session:
        assert await begin_processing(session, mid) is True
        await mark_processed(session, mid)
    reset_memory_cache()
    async with SessionLocal() as session:
        assert await begin_processing(session, mid) is False


@pytest.mark.asyncio
async def test_different_ids_independent() -> None:
    async with SessionLocal() as session:
        assert await begin_processing(session, "TEST_MSG_A") is True
        assert await begin_processing(session, "TEST_MSG_B") is True
        await mark_processed(session, "TEST_MSG_A")
        await mark_processed(session, "TEST_MSG_B")


@pytest.mark.asyncio
async def test_processing_state_retryable() -> None:
    """PROCESSING（在途，进程崩溃残留）也应可重试恢复。"""
    mid = "TEST_MSG_PROC"
    async with SessionLocal() as session:
        assert await begin_processing(session, mid) is True  # 插入 PROCESSING
        # 未 mark_processed 直接再来一次：仍 PROCESSING，可重试
        assert await begin_processing(session, mid) is True
        await mark_processed(session, mid)
        assert await begin_processing(session, mid) is False
