"""R-108：在途配对上下文不能提前消耗最终 Shadow 判定的通知游标。

真实流水线、采集器与隔离 SQLite；假 AI/准备阶段屏障，不启动通知发送器，
不下载真实媒体或调用 QQ。只通过 run_pipeline 驱动，避免依赖占位实现细节。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest_asyncio
from app.core.contracts import Sender, StandardMessage
from app.db import Base
from app.moderation.decision import ModerationDecision
from app.notifications.collector import collect_notifications
from app.notifications.models import NotificationNotice, NotificationState
from app.runtime import pipeline
from app.runtime.models import ShadowDecision
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]
FinalVerdict = Literal["allow", "record_only"]


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notification-contract.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _Barrier:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def pause(self) -> None:
        self.entered.set()
        await self.release.wait()

    async def prepare_payload(self, payload: dict[str, Any]) -> None:
        """替代可能缓慢的下载/准备步骤；不产生文件和网络请求。"""
        await self.pause()


class _Source:
    provider = "onebot"

    def __init__(self, msg: StandardMessage) -> None:
        self.msg = msg

    def parse_group_message(self, payload: dict[str, Any]) -> StandardMessage:
        return self.msg


class _FakeAI:
    def __init__(self, verdict: FinalVerdict, barrier: _Barrier | None = None) -> None:
        self.verdict = verdict
        self.barrier = barrier

    async def review_message(
        self, session: AsyncSession, msg: StandardMessage, decision: ModerationDecision, **kwargs
    ):
        if self.barrier is not None:
            await self.barrier.pause()
        return decision.model_copy(
            update={
                "verdict": self.verdict,
                "category": None,
                "confidence": 1.0,
                "recommended_actions": [],
                "reason": "合成 AI 最终结论",
            }
        ), []


def _message(when: datetime) -> StandardMessage:
    suffix = uuid.uuid4().hex
    return StandardMessage(
        provider="onebot",
        message_id=f"synthetic-message-{suffix}",
        group_openid=f"synthetic-group-{suffix}",
        sender=Sender(member_openid=f"synthetic-member-{suffix}"),
        kind="text",
        text="今天的活动已经结束，谢谢大家",
        sent_at=when,
    )


async def _run(
    sessions: SessionFactory,
    msg: StandardMessage,
    verdict: FinalVerdict,
    *,
    preparation: _Barrier | None = None,
    ai_barrier: _Barrier | None = None,
) -> ShadowDecision | None:
    async with sessions() as session:
        return await pipeline.run_pipeline(
            {"message_id": msg.message_id},
            session,
            message_source=_Source(msg),
            ai_service=_FakeAI(verdict, ai_barrier),
            dedup_key=f"onebot:synthetic-self:{msg.message_id}",
            prepare_payload=preparation.prepare_payload if preparation else None,
        )


async def _collect(
    sessions: SessionFactory, now: datetime
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    async with sessions() as session:
        await collect_notifications(
            session,
            now=now,
            business_channels=("qq",),
            fault_channels=("email",),
            health={"onebot": True},
            summary_seconds=900,
        )
        await session.commit()
        state = await session.get(NotificationState, "collector")
        assert state is not None
        summaries = (
            await session.scalars(
                select(NotificationNotice)
                .where(NotificationNotice.kind == "review_summary")
                .order_by(NotificationNotice.id)
            )
        ).all()
        return json.loads(state.value), [(notice.event_key, notice.body) for notice in summaries]


async def test_preparation_then_allow_never_generates_false_review_summary(
    sessions: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    await _collect(sessions, now)
    barrier = _Barrier()
    task = asyncio.create_task(_run(sessions, _message(now), "allow", preparation=barrier))
    try:
        await asyncio.wait_for(barrier.entered.wait(), timeout=10)
        during, notices = await _collect(sessions, now + timedelta(seconds=1))
    finally:
        barrier.release.set()
        result = await asyncio.wait_for(task, timeout=10)
    assert result is not None and result.verdict == "allow"
    assert during["review_count"] == 0 and not notices
    _, notices = await _collect(sessions, now + timedelta(seconds=901))
    assert not notices
    _, notices = await _collect(sessions, now + timedelta(seconds=1802))
    assert not notices


async def test_enabled_during_preparation_still_counts_late_final_review_once(
    sessions: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    barrier = _Barrier()
    task = asyncio.create_task(_run(sessions, _message(now), "record_only", preparation=barrier))
    try:
        await asyncio.wait_for(barrier.entered.wait(), timeout=10)
        await _collect(sessions, now)
    finally:
        barrier.release.set()
        result = await asyncio.wait_for(task, timeout=10)
    assert result is not None and result.verdict == "record_only"
    _, notices = await _collect(sessions, now + timedelta(seconds=901))
    assert len(notices) == 1 and "新增 1 条" in notices[0][1]
    _, repeated = await _collect(sessions, now + timedelta(seconds=1802))
    assert repeated == notices


async def test_out_of_order_pipeline_completion_counts_each_final_review_once(
    sessions: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    await _collect(sessions, now)
    barrier = _Barrier()
    slow = asyncio.create_task(_run(sessions, _message(now), "record_only", ai_barrier=barrier))
    try:
        await asyncio.wait_for(barrier.entered.wait(), timeout=10)
        fast = await asyncio.wait_for(
            _run(sessions, _message(now + timedelta(seconds=1)), "record_only"), timeout=10
        )
        assert fast is not None and fast.verdict == "record_only"
        _, first = await _collect(sessions, now + timedelta(seconds=901))
    finally:
        barrier.release.set()
        late = await asyncio.wait_for(slow, timeout=10)
    assert late is not None and late.verdict == "record_only"
    assert len(first) == 1 and "新增 1 条" in first[0][1]
    _, both = await _collect(sessions, now + timedelta(seconds=1802))
    assert len(both) == 2 and all("新增 1 条" in body for _, body in both)
    assert len({event for event, _ in both}) == 2
    _, repeated = await _collect(sessions, now + timedelta(seconds=2703))
    assert repeated == both
