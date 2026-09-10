"""Poll committed facts; notification collection never mutates moderation state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.orchestrator import ActionIntent
from app.cases.models import Case
from app.moderation.ai import AIUsageLog
from app.notifications.models import NotificationNotice, NotificationState
from app.notifications.service import create_notice, resolve_notice
from app.runtime.models import ShadowDecision

_FAULT_NAMES = {
    "database": "数据库读写异常",
    "backlog": "审核任务积压超过五分钟",
    "onebot": "QQ 接入未就绪",
    "moderation_worker": "审核任务停止",
    "notification_delivery": "通知通道未送达或待发积压",
}


async def _maximum(session: AsyncSession, column: Any) -> int:
    return int((await session.execute(select(func.max(column)))).scalar_one() or 0)


async def _close_finished(session: AsyncSession, now: datetime) -> None:
    for model, prefix, field, active in (
        (Case, "case:", Case.status, "PENDING_REVIEW"),
        (ActionIntent, "action:", ActionIntent.status, "UNKNOWN"),
    ):
        ids = (
            await session.execute(
                select(NotificationNotice.id)
                .join(model, NotificationNotice.event_key == prefix + cast(model.id, String))
                .where(NotificationNotice.resolved_at.is_(None), field != active)
                .limit(100)
            )
        ).scalars()
        for notice_id in ids:
            await resolve_notice(session, notice_id, now=now)


async def _faults(
    session: AsyncSession,
    state: dict[str, Any],
    health: dict[str, bool],
    channels: Sequence[str],
    now: datetime,
) -> None:
    timestamp = now.timestamp()
    faults = state.setdefault("faults", {})
    for key, healthy in health.items():
        name = _FAULT_NAMES.get(key)
        if key.startswith("ai:"):
            name = "AI 审核通道连续失败或被额度限制"
        if name is None:
            continue
        item = faults.setdefault(key, {"generation": 0})
        if not healthy:
            item.pop("good_since", None)
            item.setdefault("bad_since", timestamp)
            if timestamp - item["bad_since"] >= 90 and not item.get("notice_id"):
                item["generation"] += 1
                notice = await create_notice(
                    session,
                    event_key=f"fault:{key}:{item['generation']}",
                    kind="fault",
                    severity="page",
                    subject=name,
                    body="持续异常，请检查后台与 Windows 服务并接手。通知不改变处罚开关。",
                    channels=channels,
                    now=now,
                )
                item["notice_id"] = notice.id
        else:
            item.pop("bad_since", None)
            if item.get("notice_id"):
                item.setdefault("good_since", timestamp)
                if timestamp - item["good_since"] >= 60:
                    await resolve_notice(session, item.pop("notice_id"), now=now)
                    await create_notice(
                        session,
                        event_key=f"recovery:{key}:{item['generation']}",
                        kind="recovery",
                        severity="ticket",
                        subject=f"已恢复：{name}",
                        body="已观察到恢复信号且一分钟内无新故障。低流量不证明持续可用；请核对遗漏，不会自动恢复处罚。",
                        channels=channels,
                        now=now,
                    )
                    item.pop("good_since", None)


async def _ai_health(
    session: AsyncSession, state: dict[str, Any], current: datetime
) -> dict[str, bool]:
    """Only fresh non-cache evidence from the same model/role can recover it."""
    samples = (
        await session.execute(
            select(AIUsageLog)
            .where(
                AIUsageLog.created_at >= current - timedelta(minutes=5),
                ~AIUsageLog.source.like("cache%"),
            )
            .order_by(AIUsageLog.id.desc())
            .limit(1000)
        )
    ).scalars()
    grouped: dict[str, list[AIUsageLog]] = {}
    for sample in samples:
        role = sample.source.rsplit("_", 1)[-1] if "_" in sample.source else "auxiliary"
        # Names are internal aggregation keys, not message-controlled alert text.
        identity = hashlib.sha256(f"{sample.model_id}:{role}".encode()).hexdigest()[:24]
        grouped.setdefault(f"ai:{identity}", []).append(sample)
    states = state.setdefault("ai", {})
    for key, rows in grouped.items():
        item = states.setdefault(key, {"last_id": 0, "healthy": True})
        latest = rows[0]
        if latest.id <= item["last_id"]:
            continue
        item["last_id"] = latest.id
        if latest.ok:
            item["healthy"] = True
        elif (
            len(rows) >= 3
            and not any(row.ok for row in rows[:3])
            or state.get("faults", {}).get(key, {}).get("notice_id")
        ):
            item["healthy"] = False
    return {key: item["healthy"] for key, item in states.items()}


async def collect_notifications(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    business_channels: Sequence[str],
    fault_channels: Sequence[str],
    health: dict[str, bool],
    summary_seconds: int = 900,
) -> None:
    """Persist notices and watermarks together; caller commits the transaction.

    First enable establishes cursors and one backlog summary. Later UNKNOWN
    transitions are selected by updated_at, not their original action ID.
    """
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    row = await session.get(NotificationState, "collector")
    if row is None:
        state: dict[str, Any] = {
            "enabled_at": current.timestamp(),
            "case_cursor": await _maximum(session, Case.id),
            "shadow_cursor": await _maximum(session, ShadowDecision.id),
            "review_count": 0,
            "summary_at": current.timestamp(),
        }
        cases = int(
            (
                await session.execute(
                    select(func.count()).select_from(Case).where(Case.status == "PENDING_REVIEW")
                )
            ).scalar_one()
        )
        unknown = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ActionIntent)
                    .where(ActionIntent.status == "UNKNOWN")
                )
            ).scalar_one()
        )
        if cases or unknown:
            await create_notice(
                session,
                event_key="startup:backlog",
                kind="startup",
                severity="page",
                subject="启用通知：已有待接手事项",
                body=f"待审案件 {cases} 件，结果未知动作 {unknown} 条。请在后台核对，历史事项不逐条补发。",
                channels=business_channels,
                now=current,
            )
        row = NotificationState(key="collector", value="{}")
        session.add(row)
    else:
        state = json.loads(row.value)
        cases_new = (
            await session.execute(
                select(Case.id, Case.status)
                .where(Case.id > state["case_cursor"])
                .order_by(Case.id)
                .limit(25)
            )
        ).all()
        for case_id, status in cases_new:
            if status == "PENDING_REVIEW":
                await create_notice(
                    session,
                    event_key=f"case:{case_id}",
                    kind="case",
                    severity="page",
                    subject="有新案件需要人工决定",
                    body=f"案件记录 #{case_id}。请登录后台核验证据；确认接手不代表批准踢人。",
                    channels=business_channels,
                    now=current,
                )
            state["case_cursor"] = case_id
        enabled_at = datetime.fromtimestamp(state["enabled_at"], UTC)
        observed = (
            select(NotificationState.key)
            .where(NotificationState.key == "seen_action:" + cast(ActionIntent.id, String))
            .exists()
        )
        unknown_ids = (
            await session.execute(
                select(ActionIntent.id)
                .where(
                    ActionIntent.status == "UNKNOWN",
                    ActionIntent.updated_at >= enabled_at,
                    ~observed,
                )
                .order_by(ActionIntent.id)
                .limit(25)
            )
        ).scalars()
        for action_id in unknown_ids:
            await create_notice(
                session,
                event_key=f"action:{action_id}",
                kind="unknown_action",
                severity="page",
                subject="动作结果未知，需要人工核对",
                body=f"动作记录 #{action_id}。请对照 QQ 客户端确认，禁止直接重试或升级处罚。",
                channels=business_channels,
                now=current,
            )
            # Keep the minimal source ID watermark beyond notification retention.
            # Acknowledgement and later metadata cleanup must not rediscover an
            # UNKNOWN action. The notice and marker share the caller's transaction;
            # create_notice also preserves an existing notice during marker backfill.
            session.add(NotificationState(key=f"seen_action:{action_id}", value="1"))
        maximum = await _maximum(session, ShadowDecision.id)
        count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ShadowDecision)
                    .where(
                        ShadowDecision.id > state["shadow_cursor"],
                        ShadowDecision.id <= maximum,
                        ShadowDecision.verdict == "record_only",
                    )
                )
            ).scalar_one()
        )
        state["review_count"] += count
        state["shadow_cursor"] = maximum
        if current.timestamp() - state["summary_at"] >= summary_seconds:
            if state["review_count"]:
                await create_notice(
                    session,
                    event_key=f"summary:{maximum}",
                    kind="review_summary",
                    severity="ticket",
                    subject="疑难消息待人工复核摘要",
                    body=f"自上次汇总新增 {state['review_count']} 条仅记录消息。请到后台影子记录页复核，通知不含群内容。",
                    channels=business_channels,
                    now=current,
                )
            state["review_count"] = 0
            state["summary_at"] = current.timestamp()
    health = {**health, **await _ai_health(session, state, current)}
    await _close_finished(session, current)
    await _faults(session, state, health, fault_channels, current)
    row.value = json.dumps(state, sort_keys=True, separators=(",", ":"))
    await session.flush()
