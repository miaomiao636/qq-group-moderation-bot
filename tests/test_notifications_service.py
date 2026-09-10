"""Notification persistence and delivery safety; no real external sender."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.db import Base
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


@pytest.fixture
async def notice_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    importlib.import_module("app.notifications.models")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA journal_mode=WAL"))
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


async def _create(session, key="fault:1", *, severity="page", channels=("smtp",), now=NOW):
    service = importlib.import_module("app.notifications.service")
    return await service.create_notice(
        session,
        event_key=key,
        kind="runtime_offline",
        severity=severity,
        subject="服务需要检查",
        body="连接尚未恢复，请在管理后台查看状态。",
        channels=channels,
        now=now,
    )


def test_delivery_contracts_are_immutable() -> None:
    contracts = importlib.import_module("app.notifications.contracts")
    message = contracts.DeliveryMessage(notice_id=1, subject="服务状态", body="固定摘要")
    result = contracts.DeliveryResult(status="SENT")
    with pytest.raises(FrozenInstanceError):
        message.subject = "changed"
    with pytest.raises(FrozenInstanceError):
        result.retryable = True


@pytest.mark.parametrize("status", ["UNKNOWN", "SENT", "SKIPPED"])
def test_only_confirmed_failed_result_can_request_retry(status: str) -> None:
    contracts = importlib.import_module("app.notifications.contracts")
    with pytest.raises(ValueError, match="retryable"):
        contracts.DeliveryResult(status=status, retryable=True)


async def test_create_is_atomic_idempotent_and_does_not_commit(notice_engine: AsyncEngine) -> None:
    models = importlib.import_module("app.notifications.models")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        first = await _create(session, channels=("smtp", "smtp", "wecom"))
        again = await _create(session, channels=("additional",))
        assert first.id == again.id
        rows = list(await session.scalars(select(models.NotificationDelivery)))
        assert {(row.channel, row.audience) for row in rows} == {
            ("smtp", "primary"),
            ("wecom", "primary"),
        }
        await session.rollback()
    async with AsyncSession(notice_engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationNotice)) == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationDelivery)) == 0
        )


async def test_concurrent_creation_has_one_notice_and_delivery_per_channel(
    notice_engine: AsyncEngine,
) -> None:
    models = importlib.import_module("app.notifications.models")

    async def create_once():
        async with AsyncSession(notice_engine, expire_on_commit=False) as session:
            notice = await _create(session, channels=("smtp", "wecom"))
            await session.commit()
            return notice.id

    identifiers = await asyncio.gather(create_once(), create_once())
    assert identifiers[0] == identifiers[1]
    async with AsyncSession(notice_engine) as session:
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationNotice)) == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(models.NotificationDelivery)) == 2
        )


async def test_concurrent_claim_is_single_winner_and_requires_token(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    contracts = importlib.import_module("app.notifications.contracts")
    async with AsyncSession(notice_engine) as session:
        await _create(session)
        await session.commit()

    async def claim_once():
        async with AsyncSession(notice_engine, expire_on_commit=False) as session:
            delivery = await service.claim_delivery(session, now=NOW)
            await session.commit()
            return delivery

    claims = await asyncio.gather(claim_once(), claim_once())
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    delivery = winners[0]
    assert delivery.status == "SENDING" and delivery.attempts == 1 and delivery.claim_token
    async with AsyncSession(notice_engine) as session:
        assert not await service.complete_delivery(
            session,
            delivery.id,
            claim_token="incorrect",
            result=contracts.DeliveryResult("SENT"),
            now=NOW,
        )
        assert await service.complete_delivery(
            session,
            delivery.id,
            claim_token=delivery.claim_token,
            result=contracts.DeliveryResult("SENT"),
            now=NOW,
        )
        assert not await service.complete_delivery(
            session,
            delivery.id,
            claim_token=delivery.claim_token,
            result=contracts.DeliveryResult("FAILED", "not_sent", True),
            now=NOW,
        )


async def test_confirmed_unsent_failure_retries_at_60_300_seconds_then_stops(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    contracts = importlib.import_module("app.notifications.contracts")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        await _create(session)
        current = NOW
        for attempt, delay in ((1, 60), (2, 300), (3, None)):
            delivery = await service.claim_delivery(session, now=current)
            assert delivery is not None and delivery.attempts == attempt
            assert await service.complete_delivery(
                session,
                delivery.id,
                claim_token=delivery.claim_token,
                result=contracts.DeliveryResult("FAILED", "not_sent", True),
                now=current,
            )
            assert await service.claim_delivery(session, now=current) is None
            if delay is not None:
                assert (
                    await service.claim_delivery(
                        session, now=current + timedelta(seconds=delay - 1)
                    )
                    is None
                )
                current += timedelta(seconds=delay)
        assert await service.claim_delivery(session, now=current + timedelta(days=1)) is None


@pytest.mark.parametrize("result_status", ["UNKNOWN", "SKIPPED", "FAILED"])
async def test_terminal_delivery_results_never_auto_retry(
    notice_engine: AsyncEngine, result_status: str
) -> None:
    service = importlib.import_module("app.notifications.service")
    contracts = importlib.import_module("app.notifications.contracts")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        await _create(session)
        delivery = await service.claim_delivery(session, now=NOW)
        assert delivery is not None
        assert await service.complete_delivery(
            session,
            delivery.id,
            claim_token=delivery.claim_token,
            result=contracts.DeliveryResult(result_status, "delivery_problem"),
            now=NOW,
        )
        assert await service.claim_delivery(session, now=NOW + timedelta(days=1)) is None


async def test_expired_sending_becomes_unknown_and_rejects_stale_completion(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    models = importlib.import_module("app.notifications.models")
    contracts = importlib.import_module("app.notifications.contracts")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        await _create(session)
        delivery = await service.claim_delivery(session, now=NOW)
        assert delivery is not None
        token, identifier = delivery.claim_token, delivery.id
        await session.commit()
    async with AsyncSession(notice_engine) as session:
        later = NOW + timedelta(seconds=121)
        assert await service.reap_stale_deliveries(session, now=later) == 1
        assert await service.reap_stale_deliveries(session, now=later) == 0
        assert not await service.complete_delivery(
            session,
            identifier,
            claim_token=token,
            result=contracts.DeliveryResult("SENT"),
            now=later,
        )
        row = await session.get(models.NotificationDelivery, identifier)
        assert row.status == "UNKNOWN" and row.error_code == "lease_expired"
        assert await service.claim_delivery(session, now=later) is None


async def test_ack_cancels_pending_primary_backup_but_preserves_in_flight(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    models = importlib.import_module("app.notifications.models")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        notice = await _create(session, channels=("smtp", "wecom"))
        in_flight = await service.claim_delivery(session, now=NOW)
        assert in_flight is not None
        assert (
            await service.enqueue_escalations(
                session, channels=("smtp", "wecom"), now=NOW + timedelta(seconds=901)
            )
            == 1
        )
        assert await service.acknowledge_notice(session, notice.id, actor="human:admin", now=NOW)
        assert not await service.acknowledge_notice(session, notice.id, actor="other", now=NOW)
        deliveries = list(
            await session.scalars(
                select(models.NotificationDelivery).order_by(models.NotificationDelivery.id)
            )
        )
        assert deliveries[0].status == "SENDING"
        assert [delivery.status for delivery in deliveries[1:]] == ["SKIPPED"] * 3
        assert notice.acknowledged_by == "human:admin"
        assert await service.claim_delivery(session, now=NOW + timedelta(hours=1)) is None


async def test_only_unacknowledged_unresolved_pages_escalate_once(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    models = importlib.import_module("app.notifications.models")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        await _create(session, "open")
        acknowledged = await _create(session, "acknowledged")
        resolved = await _create(session, "resolved")
        await _create(session, "ticket", severity="ticket")
        await service.acknowledge_notice(session, acknowledged.id, actor="human", now=NOW)
        assert await service.resolve_notice(session, resolved.id, now=NOW)
        assert not await service.resolve_notice(session, resolved.id, now=NOW)
        assert not await service.acknowledge_notice(session, resolved.id, actor="human", now=NOW)
        assert (
            await service.enqueue_escalations(
                session, channels=("smtp",), now=NOW + timedelta(seconds=899)
            )
            == 0
        )
        assert (
            await service.enqueue_escalations(
                session, channels=(), now=NOW + timedelta(seconds=900)
            )
            == 0
        )
        assert (
            await service.enqueue_escalations(
                session, channels=("smtp", "smtp"), now=NOW + timedelta(seconds=900)
            )
            == 1
        )
        assert (
            await service.enqueue_escalations(
                session, channels=("smtp",), now=NOW + timedelta(days=1)
            )
            == 0
        )
        backup = list(
            await session.scalars(
                select(models.NotificationDelivery).where(
                    models.NotificationDelivery.audience == "backup"
                )
            )
        )
        assert len(backup) == 1


async def test_concurrent_escalation_enqueues_backup_only_once(notice_engine: AsyncEngine) -> None:
    service = importlib.import_module("app.notifications.service")
    models = importlib.import_module("app.notifications.models")
    async with AsyncSession(notice_engine) as session:
        await _create(session)
        await session.commit()

    async def escalate_once():
        async with AsyncSession(notice_engine) as session:
            count = await service.enqueue_escalations(
                session, channels=("smtp",), now=NOW + timedelta(minutes=16)
            )
            await session.commit()
            return count

    assert sorted(await asyncio.gather(escalate_once(), escalate_once())) == [0, 1]
    async with AsyncSession(notice_engine) as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(models.NotificationDelivery)
                .where(models.NotificationDelivery.audience == "backup")
            )
            == 1
        )


async def test_purge_preserves_open_notices_inflight_and_collector_watermarks(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    models = importlib.import_module("app.notifications.models")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        old = NOW - timedelta(days=200)
        acknowledged = await _create(session, "old-ack", now=old)
        await service.acknowledge_notice(session, acknowledged.id, actor="human", now=NOW)
        inflight_notice = await _create(session, "old-inflight", now=old)
        inflight = await service.claim_delivery(session, now=NOW)
        assert inflight is not None
        await service.resolve_notice(session, inflight_notice.id, now=NOW)
        await _create(session, "old-open", now=old)
        await _create(session, "recent", now=NOW)
        session.add(models.NotificationState(key="collector:test", value='{"last_id":10}'))
        await session.flush()

        counts = await service.purge_notifications(session, before=NOW - timedelta(days=180))

        assert counts == {"notices_deleted": 1, "deliveries_deleted": 1}
        notices = list(await session.scalars(select(models.NotificationNotice)))
        assert {notice.event_key for notice in notices} == {"old-inflight", "old-open", "recent"}
        assert (
            await session.get(models.NotificationState, "collector:test")
        ).value == '{"last_id":10}'


async def test_ack_during_send_prevents_failed_result_from_scheduling_retry(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    contracts = importlib.import_module("app.notifications.contracts")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        notice = await _create(session)
        delivery = await service.claim_delivery(session, now=NOW)
        assert delivery is not None
        identifier, token = delivery.id, delivery.claim_token
        assert await service.acknowledge_notice(session, notice.id, actor="human", now=NOW)
        assert await service.complete_delivery(
            session,
            identifier,
            claim_token=token,
            result=contracts.DeliveryResult("FAILED", "not_sent", True),
            now=NOW,
        )
        await session.refresh(delivery)
        assert delivery.next_attempt_at is None
        assert await service.claim_delivery(session, now=NOW + timedelta(days=1)) is None


async def test_late_completion_without_reaper_is_unknown(notice_engine: AsyncEngine) -> None:
    service = importlib.import_module("app.notifications.service")
    contracts = importlib.import_module("app.notifications.contracts")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        await _create(session)
        delivery = await service.claim_delivery(session, now=NOW)
        assert delivery is not None
        assert not await service.complete_delivery(
            session,
            delivery.id,
            claim_token=delivery.claim_token,
            result=contracts.DeliveryResult("FAILED", "not_sent", True),
            now=NOW + timedelta(seconds=120),
        )
        await session.refresh(delivery)
        assert delivery.status == "UNKNOWN" and delivery.next_attempt_at is None


async def test_claim_rollback_releases_unsent_work_without_consuming_attempt(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    async with AsyncSession(notice_engine) as session:
        await _create(session)
        await session.commit()
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        first = await service.claim_delivery(session, now=NOW)
        assert first is not None
        first_token = first.claim_token
        await session.rollback()
    async with AsyncSession(notice_engine) as session:
        second = await service.claim_delivery(session, now=NOW)
        assert second is not None and second.attempts == 1
        assert second.claim_token != first_token


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_key", "event/unsafe"),
        ("kind", ""),
        ("severity", "other"),
        ("subject", "title\r\nBcc:unexpected"),
        ("body", "unexpected\0control"),
        ("body", "x" * 2001),
        ("channels", "smtp"),
    ],
)
async def test_notification_inputs_are_bounded(
    notice_engine: AsyncEngine, field: str, value
) -> None:
    service = importlib.import_module("app.notifications.service")
    arguments = dict(
        event_key="bounded:1",
        kind="runtime_offline",
        severity="page",
        subject="固定摘要",
        body="固定正文",
        channels=("smtp",),
        now=NOW,
    )
    arguments[field] = value
    async with AsyncSession(notice_engine) as session:
        with pytest.raises(ValueError):
            await service.create_notice(session, **arguments)


async def test_concurrent_ack_and_escalation_leave_no_pending_notifications(
    notice_engine: AsyncEngine,
) -> None:
    service = importlib.import_module("app.notifications.service")
    models = importlib.import_module("app.notifications.models")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        notice = await _create(session)
        notice_id = notice.id
        await session.commit()

    async def acknowledge():
        async with AsyncSession(notice_engine) as session:
            changed = await service.acknowledge_notice(session, notice_id, actor="human", now=NOW)
            await session.commit()
            return changed

    async def escalate():
        async with AsyncSession(notice_engine) as session:
            changed = await service.enqueue_escalations(
                session, channels=("smtp",), now=NOW + timedelta(minutes=16)
            )
            await session.commit()
            return changed

    acknowledged, _ = await asyncio.gather(acknowledge(), escalate())
    assert acknowledged
    async with AsyncSession(notice_engine) as session:
        deliveries = list(await session.scalars(select(models.NotificationDelivery)))
        assert all(delivery.status == "SKIPPED" for delivery in deliveries)
        assert len([delivery for delivery in deliveries if delivery.audience == "backup"]) <= 1


async def test_ack_cancels_previously_scheduled_retry(notice_engine: AsyncEngine) -> None:
    service = importlib.import_module("app.notifications.service")
    contracts = importlib.import_module("app.notifications.contracts")
    async with AsyncSession(notice_engine, expire_on_commit=False) as session:
        notice = await _create(session)
        delivery = await service.claim_delivery(session, now=NOW)
        assert delivery is not None
        assert await service.complete_delivery(
            session,
            delivery.id,
            claim_token=delivery.claim_token,
            result=contracts.DeliveryResult("FAILED", "not_sent", True),
            now=NOW,
        )
        assert await service.acknowledge_notice(session, notice.id, actor="human", now=NOW)
        await session.refresh(delivery)
        assert delivery.status == "SKIPPED" and delivery.next_attempt_at is None
        assert await service.claim_delivery(session, now=NOW + timedelta(minutes=10)) is None
