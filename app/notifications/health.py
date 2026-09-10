"""Private readiness: a quiet group is not a failed transport.

Use a short-lived SQLite probe connection with a two-second busy timeout so a
locked production database cannot indefinitely block an external watchdog.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import SystemMeta
from app.notifications.models import NotificationDelivery, NotificationNotice
from app.runtime.inbox import InboxEvent


async def database_checks(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, bool]:
    try:
        async with asyncio.timeout(4):
            async with factory() as session:
                now = datetime.now(UTC)
                await session.execute(
                    insert(SystemMeta)
                    .values(key="notification_health", value=now.isoformat(), created_at=now)
                    .on_conflict_do_update(
                        index_elements=[SystemMeta.key], set_={"value": now.isoformat()}
                    )
                )
                oldest = (
                    await session.execute(
                        select(func.min(InboxEvent.created_at)).where(
                            InboxEvent.status.in_(("PENDING", "PROCESSING"))
                        )
                    )
                ).scalar_one()
                failed_delivery = (
                    await session.execute(
                        select(NotificationDelivery.id)
                        .join(NotificationNotice)
                        .where(
                            NotificationNotice.acknowledged_at.is_(None),
                            NotificationNotice.resolved_at.is_(None),
                            or_(
                                and_(
                                    NotificationDelivery.channel == "email",
                                    or_(
                                        NotificationDelivery.status == "UNKNOWN",
                                        and_(
                                            NotificationDelivery.status == "FAILED",
                                            NotificationDelivery.next_attempt_at.is_(None),
                                        ),
                                    ),
                                ),
                                and_(
                                    NotificationDelivery.status.in_(
                                        ("PENDING", "SENDING", "FAILED")
                                    ),
                                    NotificationDelivery.created_at < now - timedelta(minutes=5),
                                ),
                            ),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                await session.commit()
                backlog_ok = oldest is None or oldest.replace(tzinfo=None) >= (
                    now - timedelta(minutes=5)
                ).replace(tzinfo=None)
                return {
                    "database": True,
                    "backlog": backlog_ok,
                    "notification_delivery": failed_delivery is None,
                }
    except Exception:  # noqa: BLE001 - return fixed health codes, never DB URLs/errors
        return {"database": False, "backlog": False, "notification_delivery": False}


def evaluate_readiness(
    *,
    database: dict[str, bool],
    onebot_enabled: bool,
    onebot_ready: bool,
    worker_alive: bool,
    notifications_enabled: bool,
    notification_worker_alive: bool,
) -> dict[str, object]:
    checks = {
        **database,
        "onebot": onebot_enabled and onebot_ready,
        "moderation_worker": onebot_enabled and worker_alive,
        "notification_worker": notifications_enabled and notification_worker_alive,
    }
    return {"ready": all(checks.values()), "checks": checks}


async def check_readiness() -> dict[str, object]:
    from app.notifications.runtime import notification_worker_healthy
    from app.runtime.onebot_ws import moderation_worker_healthy, onebot_status

    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        connect_args={"timeout": 2},
        poolclass=NullPool,
        hide_parameters=True,
    )
    try:
        database = await database_checks(async_sessionmaker(engine))
    finally:
        await engine.dispose()
    return evaluate_readiness(
        database=database,
        onebot_enabled=settings.onebot_ws_enabled,
        onebot_ready=onebot_status.state() == "ready",
        worker_alive=moderation_worker_healthy(),
        notifications_enabled=True,
        notification_worker_alive=notification_worker_healthy(),
    )
