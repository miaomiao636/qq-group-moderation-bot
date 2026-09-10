"""Business signals are copied as safe summaries, never as action commands."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.actions.orchestrator import ActionIntent
from app.cases.models import Case
from app.db import Base
from app.moderation.ai import AIUsageLog
from app.notifications.collector import collect_notifications
from app.notifications.models import NotificationNotice
from app.runtime.models import ShadowDecision
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


async def collect(session, now, health=None):
    await collect_notifications(
        session,
        now=now,
        business_channels=("qq",),
        fault_channels=("email",),
        health=health or {"onebot": True},
        summary_seconds=900,
    )
    await session.commit()


@pytest.mark.asyncio
async def test_startup_summarizes_history_and_does_not_dump_private_content(session):
    now = datetime.now(UTC)
    session.add(
        Case(
            case_no="private-case-text",
            group_openid="private-group",
            member_openid="private-member",
        )
    )
    await session.commit()
    await collect(session, now)
    await collect(session, now + timedelta(seconds=30))
    notices = (await session.execute(select(NotificationNotice))).scalars().all()
    assert len(notices) == 1 and notices[0].kind == "startup"
    assert "private" not in notices[0].body


@pytest.mark.asyncio
async def test_new_case_and_unknown_transition_dedup_and_never_change_actions(session):
    now = datetime.now(UTC)
    intent = ActionIntent(
        idempotency_key="original",
        action="recall",
        group_openid="private",
        status="EXECUTING",
        created_at=now - timedelta(minutes=10),
    )
    session.add(intent)
    await session.commit()
    await collect(session, now)
    case = Case(case_no="unsafe<CQ:at,qq=all>", group_openid="secret", member_openid="secret")
    session.add(case)
    intent.status = "UNKNOWN"
    intent.updated_at = now + timedelta(seconds=1)
    await session.commit()
    await collect(session, now + timedelta(seconds=30))
    await collect(session, now + timedelta(seconds=60))
    notices = (await session.execute(select(NotificationNotice))).scalars().all()
    assert sorted(n.kind for n in notices) == ["case", "unknown_action"]
    assert all("secret" not in n.body and "CQ:" not in n.body for n in notices)
    await session.refresh(intent)
    assert intent.status == "UNKNOWN"
    case.status = "CLOSED"
    intent.status = "SUCCESS"
    await session.commit()
    await collect(session, now + timedelta(seconds=90))
    await session.refresh(notices[0])
    assert all(
        n.resolved_at is not None
        for n in (await session.execute(select(NotificationNotice))).scalars()
    )


@pytest.mark.asyncio
async def test_record_only_is_batched_and_restart_does_not_reset_digest(session):
    now = datetime.now(UTC)
    await collect(session, now)
    for index in range(3):
        session.add(
            ShadowDecision(
                message_id=f"s{index}",
                group_openid="private",
                member_openid="private",
                verdict="record_only",
                detail_json=json.dumps({"raw": "private"}),
            )
        )
    await session.commit()
    await collect(session, now + timedelta(seconds=60))
    assert not (await session.execute(select(NotificationNotice))).scalars().all()
    await collect(session, now + timedelta(seconds=901))
    await collect(session, now + timedelta(seconds=950))
    notices = (await session.execute(select(NotificationNotice))).scalars().all()
    assert len(notices) == 1 and notices[0].kind == "review_summary"
    assert "3" in notices[0].body and "private" not in notices[0].body


@pytest.mark.asyncio
async def test_fault_debounce_recovery_and_new_incident(session):
    now = datetime.now(UTC)
    await collect(session, now, {"onebot": False})
    await collect(session, now + timedelta(seconds=60), {"onebot": False})
    assert not (await session.execute(select(NotificationNotice))).scalars().all()
    await collect(session, now + timedelta(seconds=90), {"onebot": False})
    await collect(session, now + timedelta(seconds=120), {"onebot": False})
    assert len((await session.execute(select(NotificationNotice))).scalars().all()) == 1
    await collect(session, now + timedelta(seconds=150))
    await collect(session, now + timedelta(seconds=210))
    notices = (
        (await session.execute(select(NotificationNotice).order_by(NotificationNotice.id)))
        .scalars()
        .all()
    )
    assert [n.kind for n in notices] == ["fault", "recovery"]
    assert notices[0].resolved_at is not None
    await collect(session, now + timedelta(seconds=240), {"onebot": False})
    await collect(session, now + timedelta(seconds=330), {"onebot": False})
    assert len((await session.execute(select(NotificationNotice))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_ai_fault_not_recovered_by_silence_cache_or_different_model(session):
    now = datetime.now(UTC)
    await collect(session, now)
    for index in range(3):
        session.add(
            AIUsageLog(
                model_id="failing-model",
                source="error_primary",
                group_openid="private",
                message_id=f"ai{index}",
                ok=False,
                created_at=now,
            )
        )
    session.add(
        AIUsageLog(
            model_id="other-model",
            source="vision_secondary",
            group_openid="private",
            message_id="other",
            ok=True,
            created_at=now,
        )
    )
    await session.commit()
    await collect(session, now + timedelta(seconds=30))
    await collect(session, now + timedelta(seconds=120))
    notices = (await session.execute(select(NotificationNotice))).scalars().all()
    assert len(notices) == 1 and notices[0].kind == "fault"
    session.add(
        AIUsageLog(
            model_id="failing-model",
            source="cache_primary",
            group_openid="private",
            message_id="cached",
            ok=True,
            created_at=now + timedelta(seconds=130),
        )
    )
    await session.commit()
    await collect(session, now + timedelta(seconds=150))
    await collect(session, now + timedelta(seconds=601))
    await session.refresh(notices[0])
    assert notices[0].resolved_at is None
    session.add(
        AIUsageLog(
            model_id="failing-model",
            source="vision_primary",
            group_openid="private",
            message_id="successful",
            ok=True,
            created_at=now + timedelta(seconds=620),
        )
    )
    await session.commit()
    await collect(session, now + timedelta(seconds=630))
    await collect(session, now + timedelta(seconds=691))
    await session.refresh(notices[0])
    assert notices[0].resolved_at is not None
