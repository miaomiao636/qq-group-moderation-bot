"""Database-backed stop shared by the web service and action workers."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAudit, SystemSetting

STOP_KEY = "runtime_emergency_stop"
REVISION_KEY = "runtime_emergency_stop_revision"


async def emergency_stop_active(session: AsyncSession) -> bool:
    """Fresh connection avoids a caller's identity map and long-lived WAL snapshot."""
    return bool((await emergency_stop_state(session))["active"])


async def emergency_stop_state(session: AsyncSession) -> dict[str, Any]:
    """Every stop activation changes its revision, invalidating older resume plans."""
    try:
        async with AsyncSession(bind=session.bind) as reader:
            values = {
                key: value
                for key, value in (
                    await reader.execute(
                        select(SystemSetting.key, SystemSetting.value).where(
                            SystemSetting.key.in_((STOP_KEY, REVISION_KEY))
                        )
                    )
                ).all()
            }
            return {
                "active": values.get(STOP_KEY) not in (None, "false"),
                "revision": values.get(REVISION_KEY, ""),
            }
    except Exception:  # noqa: BLE001 - unreadable control state must stop external effects
        return {"active": True, "revision": "unavailable"}


async def set_emergency_stop(session: AsyncSession, active: bool, *, actor: str) -> None:
    """Caller authorizes resumption; activation never waits for a second confirmation."""
    statement = insert(SystemSetting).values(key=STOP_KEY, value="true" if active else "false")
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[SystemSetting.key], set_={"value": statement.excluded.value}
        )
    )
    revision = insert(SystemSetting).values(key=REVISION_KEY, value=uuid.uuid4().hex)
    await session.execute(
        revision.on_conflict_do_update(
            index_elements=[SystemSetting.key], set_={"value": revision.excluded.value}
        )
    )
    session.add(
        AdminAudit(
            operator=actor,
            action="emergency_stop" if active else "emergency_resume",
            target_type="system",
            target_id=STOP_KEY,
            detail_json=json.dumps({"active": active}),
        )
    )
    await session.commit()
