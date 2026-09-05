"""T-102 幂等去重测试：重复事件不得重复处理（防重复处罚的关键防线）。"""

from __future__ import annotations

import pytest
from app.adapters.qq_official.dedup import check_and_mark, reset_memory_cache
from app.db import SessionLocal


@pytest.fixture(autouse=True)
def _clean_memory() -> None:
    reset_memory_cache()
    yield
    reset_memory_cache()


@pytest.mark.asyncio
async def test_first_seen_returns_true_then_duplicates_false() -> None:
    async with SessionLocal() as session:
        assert await check_and_mark(session, "TEST_MSG_DUP_1") is True
        assert await check_and_mark(session, "TEST_MSG_DUP_1") is False
        assert await check_and_mark(session, "TEST_MSG_DUP_1") is False


@pytest.mark.asyncio
async def test_dedup_persists_across_sessions() -> None:
    async with SessionLocal() as session:
        assert await check_and_mark(session, "TEST_MSG_PERSIST") is True
    # 新会话（模拟进程内重启后的新连接；内存缓存仍在，再清空验证数据库防线）
    reset_memory_cache()
    async with SessionLocal() as session:
        assert await check_and_mark(session, "TEST_MSG_PERSIST") is False


@pytest.mark.asyncio
async def test_different_ids_independent() -> None:
    async with SessionLocal() as session:
        assert await check_and_mark(session, "TEST_MSG_A") is True
        assert await check_and_mark(session, "TEST_MSG_B") is True


@pytest.mark.asyncio
async def test_memory_cache_fast_path() -> None:
    async with SessionLocal() as session:
        assert await check_and_mark(session, "TEST_MSG_MEM") is True
        # 第二次调用命中内存缓存，直接拒绝（结果与数据库防线一致）
        assert await check_and_mark(session, "TEST_MSG_MEM") is False
