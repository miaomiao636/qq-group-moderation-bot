"""Guarded official action orchestration.

T-106: default SHADOW mode never calls QQ official actions. OFFICIAL mode first
persists idempotent action intents, then calls recall/mute/warn only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, Protocol

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.qq_official.actions import ActionResult, OfficialActionAdapter
from app.adapters.qq_official.audit import log_action
from app.adapters.qq_official.auth import TokenManager
from app.adapters.qq_official.contract import StandardMessage
from app.cases.service import record_violation
from app.config import Settings, get_settings
from app.db import Base
from app.moderation.decision import ModerationDecision

ActionMode = Literal["SHADOW", "OFFICIAL"]
IntentStatus = Literal["PENDING", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN", "SKIPPED"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class OfficialActionClient(Protocol):
    async def recall(
        self, group_openid: str, message_id: str, *, actor: str = "system"
    ) -> ActionResult:
        """Recall a message."""

    async def mute(
        self, group_openid: str, member_openid: str, seconds: int, *, actor: str = "system"
    ) -> ActionResult:
        """Mute a member."""

    async def warn(
        self,
        group_openid: str,
        reply_to_message_id: str,
        text: str,
        *,
        msg_seq: int = 1,
        actor: str = "system",
    ) -> ActionResult:
        """Send one passive warning reply."""


class ActionIntent(Base):
    """Persistent idempotency record for one official moderation action."""

    __tablename__ = "action_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    group_openid: Mapped[str] = mapped_column(String(128), index=True)
    target_member_openid: Mapped[str] = mapped_column(String(128), default="")
    message_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str] = mapped_column(String(255), default="")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


async def orchestrate_actions(
    session: AsyncSession,
    msg: StandardMessage,
    decision: ModerationDecision,
    *,
    official_client: OfficialActionClient | None = None,
    settings: Settings | None = None,
    actor: str = "system",
) -> list[ActionIntent]:
    """Persist and execute official recall/mute/warn in guarded OFFICIAL mode."""
    settings = settings or get_settings()
    if settings.action_mode != "OFFICIAL":
        return []
    existing = await _existing_message_intents(session, msg.message_id)
    if existing:
        return existing
    if settings.emergency_stop:
        return [await _record_skipped(session, msg, "recall", actor, "急停开关开启，禁止外部动作")]
    if decision.verdict != "violation_high":
        return []
    if decision.is_protected_sender or msg.sender.role in ("owner", "admin"):
        return [await _record_skipped(session, msg, "recall", actor, "保护角色，编排层拦截")]
    if not decision.recommended_actions:
        return []

    outcome = await record_violation(session, msg, decision)
    client = official_client or _build_official_client(settings)
    intents: list[ActionIntent] = []
    for planned in outcome.planned_actions:
        intent = await _create_intent(session, msg, planned.action, planned.params, actor)
        intents.append(intent)
        if intent.status != "PENDING":
            continue
        result = await _execute_intent(session, client, intent)
        await log_action(
            session,
            result,
            group_openid=msg.group_openid,
            target_member_openid=msg.sender.member_openid if planned.action == "mute" else "",
            message_id=msg.message_id,
            actor=actor,
        )
        if intent.status == "UNKNOWN":
            break
    return intents


def summarize_intents(intents: list[ActionIntent]) -> list[dict[str, Any]]:
    """Return a JSON-safe summary for decision detail records."""
    return [
        {
            "id": intent.id,
            "action": intent.action,
            "status": intent.status,
            "message_id": intent.message_id,
            "target_member_openid": intent.target_member_openid,
            "reason": intent.reason,
        }
        for intent in intents
    ]


async def _existing_message_intents(session: AsyncSession, message_id: str) -> list[ActionIntent]:
    return list(
        (
            await session.execute(
                select(ActionIntent)
                .where(ActionIntent.message_id == message_id)
                .order_by(ActionIntent.id.asc())
            )
        )
        .scalars()
        .all()
    )


async def _record_skipped(
    session: AsyncSession,
    msg: StandardMessage,
    action: str,
    actor: str,
    reason: str,
) -> ActionIntent:
    key = _intent_key(msg.message_id, action, {"reason": reason})
    existing = await session.scalar(select(ActionIntent).where(ActionIntent.idempotency_key == key))
    if existing is not None:
        return existing
    intent = ActionIntent(
        idempotency_key=key,
        action=action,
        status="SKIPPED",
        group_openid=msg.group_openid,
        target_member_openid=msg.sender.member_openid,
        message_id=msg.message_id,
        params_json=json.dumps({"reason": reason}, ensure_ascii=False),
        reason=reason,
        actor=actor,
    )
    session.add(intent)
    await session.commit()
    return intent


async def _create_intent(
    session: AsyncSession,
    msg: StandardMessage,
    action: str,
    params: dict[str, Any],
    actor: str,
) -> ActionIntent:
    if action not in ("recall", "mute", "warn"):
        raise ValueError(f"非法官方动作: {action}")
    key = _intent_key(msg.message_id, action, params)
    existing = await session.scalar(select(ActionIntent).where(ActionIntent.idempotency_key == key))
    if existing is not None:
        return existing
    intent = ActionIntent(
        idempotency_key=key,
        action=action,
        status="PENDING",
        group_openid=msg.group_openid,
        target_member_openid=str(params.get("member_openid") or ""),
        message_id=msg.message_id,
        params_json=json.dumps(params, ensure_ascii=False, sort_keys=True),
        actor=actor,
    )
    session.add(intent)
    await session.commit()
    return intent


async def _execute_intent(
    session: AsyncSession,
    client: OfficialActionClient,
    intent: ActionIntent,
) -> ActionResult:
    intent.status = "EXECUTING"
    intent.updated_at = _utcnow()
    await session.commit()
    params = json.loads(intent.params_json)
    try:
        if intent.action == "recall":
            result = await client.recall(intent.group_openid, intent.message_id, actor=intent.actor)
        elif intent.action == "mute":
            result = await client.mute(
                intent.group_openid,
                intent.target_member_openid,
                int(params.get("seconds") or 0),
                actor=intent.actor,
            )
        elif intent.action == "warn":
            result = await client.warn(
                intent.group_openid,
                str(params.get("reply_to_message_id") or intent.message_id),
                str(params.get("text") or ""),
                actor=intent.actor,
            )
        else:
            raise ValueError(f"不支持的动作: {intent.action}")
    except Exception as exc:  # noqa: BLE001 - 外部动作状态不确定，必须冻结等待人工
        result = ActionResult(
            action=intent.action,
            ok=False,
            err_message=f"动作结果未知: {type(exc).__name__}",
            attempts=1,
        )
        intent.status = "UNKNOWN"
        intent.reason = "外部动作结果未知，禁止自动重放"
    else:
        intent.status = "SUCCEEDED" if result.ok else "FAILED"
        intent.reason = "" if result.ok else result.err_message[:255]
    intent.result_json = result.model_dump_json()
    intent.updated_at = _utcnow()
    await session.commit()
    return result


def _intent_key(message_id: str, action: str, params: dict[str, Any]) -> str:
    raw = json.dumps(
        {"message_id": message_id, "action": action, "params": params},
        ensure_ascii=False,
        sort_keys=True,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _build_official_client(settings: Settings) -> OfficialActionAdapter:
    token_manager = TokenManager(settings.qq_app_id, settings.qq_app_secret)
    return OfficialActionAdapter(token_manager, api_base=settings.qq_api_base)
