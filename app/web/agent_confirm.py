"""Human-approved immutable plans; the Agent credential is never approval authority."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAudit, AdminChangePlan

CONFIRM_TTL_SECONDS = 300
HIGH_RISK_ACTIONS = {"group_settings", "rule_publish", "rule_rollback", "emergency_resume"}


def canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


async def create_confirmation(
    session: AsyncSession,
    *,
    action: str,
    params: dict[str, Any],
    expected_state: dict[str, Any],
    requestor: str,
) -> AdminChangePlan:
    if action not in HIGH_RISK_ACTIONS:
        raise ValueError("不支持的管理计划")
    plan = AdminChangePlan(
        id=secrets.token_urlsafe(32),
        action=action,
        requestor=requestor,
        params_json=canonical(params),
        expected_state_json=canonical(expected_state),
        expires_at=datetime.now(UTC) + timedelta(seconds=CONFIRM_TTL_SECONDS),
    )
    session.add(plan)
    await session.flush()
    session.add(
        AdminAudit(
            operator=requestor,
            action="plan_create",
            target_type="admin_plan",
            target_id=plan.id,
            detail_json=canonical(
                {"action": action, "params": params, "expected_state": expected_state}
            ),
        )
    )
    await session.commit()
    return plan


async def approve_confirmation(session: AsyncSession, plan_id: str, *, human: str) -> bool:
    """Only an authenticated human route may call this function."""
    if not human.startswith("human:"):
        return False
    result = await session.execute(
        update(AdminChangePlan)
        .where(
            AdminChangePlan.id == plan_id,
            AdminChangePlan.status == "PENDING",
            AdminChangePlan.expires_at > datetime.now(UTC),
        )
        .values(status="APPROVED", approved_by=human)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return False
    session.add(
        AdminAudit(
            operator=human, action="plan_approve", target_type="admin_plan", target_id=plan_id
        )
    )
    await session.commit()
    return True


async def claim_confirmation(
    session: AsyncSession,
    plan_id: str,
    *,
    requestor: str,
    action: str | None = None,
    params: dict[str, Any] | None = None,
) -> AdminChangePlan | None:
    """Atomic single consumer; a stolen plan ID is not sufficient authority."""
    predicates = [
        AdminChangePlan.id == plan_id,
        AdminChangePlan.requestor == requestor,
        AdminChangePlan.status == "APPROVED",
        AdminChangePlan.approved_by != "",
        AdminChangePlan.expires_at > datetime.now(UTC),
    ]
    if action is not None:
        predicates.append(AdminChangePlan.action == action)
    if params is not None:
        predicates.append(AdminChangePlan.params_json == canonical(params))
    result = await session.execute(
        update(AdminChangePlan).where(*predicates).values(status="EXECUTING")
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return None
    await session.commit()
    return await session.get(AdminChangePlan, plan_id, populate_existing=True)
