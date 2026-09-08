"""Guarded moderation action orchestration（传输中立，T-305）。

T-106 引入的 SHADOW/OFFICIAL 编排保持不变；T-305 变更：
- 契约与客户端协议来自 ``app.core.contracts``（本模块顶层不再导入任何供应商
  Adapter；官方客户端仅在 OFFICIAL 分支内经 ``app.actions.official_wiring``
  惰性构建——这是组合根 seam，不是核心对 Adapter 的依赖）。
- 动作路由按群显式选择 provider（``app.core.routing``）；每群只有一个自动
  动作出口。解析出的 provider 没有可用客户端时只记录 SKIPPED 意图，绝不
  回退到官方通道，也不把 OneBot 群当成官方群处理。
- ActionIntent 与 ActionLog 双写旧镜像列与中立身份列（expand 阶段）。

默认 SHADOW 模式从不调用外部动作；任何路径都不产生踢人动作。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.cases.service import record_violation
from app.config import Settings, get_settings
from app.core.contracts import ActionResult, ModerationActionClient, StandardMessage
from app.core.routing import resolve_action_provider
from app.db import Base
from app.models import ActionLog
from app.moderation.decision import ModerationDecision

ActionMode = Literal["SHADOW", "OFFICIAL"]
IntentStatus = Literal["PENDING", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN", "SKIPPED"]

# 兼容别名：既有调用方（pipeline/tests）导入名保持不变。
OfficialActionClient = ModerationActionClient


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ActionIntent(Base):
    """Persistent idempotency record for one moderation action."""

    __tablename__ = "action_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    action: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    group_openid: Mapped[str] = mapped_column(String(128), index=True)  # 旧镜像，contract阶段移除
    target_member_openid: Mapped[str] = mapped_column(String(128), default="")  # 旧镜像
    message_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    # T-305 传输中立身份（与镜像字段双写，权威读取口径）
    provider: Mapped[str] = mapped_column(
        String(16), default="qq_official", server_default="qq_official"
    )
    external_group_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    external_user_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    external_message_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
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
    official_client: ModerationActionClient | None = None,
    action_client: ModerationActionClient | None = None,
    settings: Settings | None = None,
    actor: str = "system",
) -> list[ActionIntent]:
    """Persist and execute recall/mute/warn in guarded OFFICIAL mode.

    T-305：先按群解析动作出口 provider；``qq_official`` 使用 ``official_client``
    （缺省时经 wiring 惰性构建官方 Adapter），其他 provider 必须显式传入
    ``action_client``，否则只记录 SKIPPED 意图——绝不跨通道借用官方客户端。
    """
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

    provider = await resolve_action_provider(session, msg.external_group_id)
    if provider == "qq_official":
        client = (
            official_client if official_client is not None else _default_official_client(settings)
        )
    else:
        client = action_client
    if client is None:
        return [
            await _record_skipped(
                session,
                msg,
                "recall",
                actor,
                f"群 {msg.external_group_id} 动作出口 provider={provider} 未配置客户端",
                provider=provider,
            )
        ]

    outcome = await record_violation(session, msg, decision)
    intents: list[ActionIntent] = []
    for planned in outcome.planned_actions:
        intent = await _create_intent(
            session, msg, planned.action, planned.params, actor, provider=provider
        )
        intents.append(intent)
        if intent.status != "PENDING":
            continue
        result = await _execute_intent(session, client, intent)
        await _log_action_result(session, intent, result, actor=actor)
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
            "provider": intent.provider,
            "message_id": intent.message_id,
            "external_group_id": intent.external_group_id,
            "external_user_id": intent.external_user_id,
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
    *,
    provider: str = "qq_official",
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
        provider=provider,
        external_group_id=msg.external_group_id,
        external_user_id=msg.external_user_id,
        external_message_id=msg.message_id,
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
    *,
    provider: str = "qq_official",
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
        provider=provider,
        external_group_id=msg.external_group_id,
        external_user_id=str(params.get("member_openid") or msg.external_user_id),
        external_message_id=msg.message_id,
        params_json=json.dumps(params, ensure_ascii=False, sort_keys=True),
        actor=actor,
    )
    session.add(intent)
    await session.commit()
    return intent


async def _execute_intent(
    session: AsyncSession,
    client: ModerationActionClient,
    intent: ActionIntent,
) -> ActionResult:
    intent.status = "EXECUTING"
    intent.updated_at = _utcnow()
    await session.commit()
    params = json.loads(intent.params_json)
    try:
        if intent.action == "recall":
            result = await client.recall(
                intent.external_group_id or intent.group_openid,
                intent.external_message_id or intent.message_id,
                actor=intent.actor,
            )
        elif intent.action == "mute":
            result = await client.mute(
                intent.external_group_id or intent.group_openid,
                intent.external_user_id or intent.target_member_openid,
                int(params.get("seconds") or 0),
                actor=intent.actor,
            )
        elif intent.action == "warn":
            result = await client.warn(
                intent.external_group_id or intent.group_openid,
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


async def _log_action_result(
    session: AsyncSession,
    intent: ActionIntent,
    result: ActionResult,
    *,
    actor: str = "system",
) -> None:
    """把动作结果写入中立审计表（原官方 audit.log_action 的中立版）。"""
    session.add(
        ActionLog(
            action=result.action,
            group_openid=intent.group_openid,
            target_member_openid=intent.target_member_openid,
            message_id=intent.message_id,
            provider=intent.provider,
            external_group_id=intent.external_group_id,
            external_user_id=intent.external_user_id,
            external_message_id=intent.external_message_id,
            ok=result.ok,
            status_code=result.status_code,
            err_code=result.err_code,
            err_message=result.err_message[:500],
            attempts=result.attempts,
            actor=actor,
        )
    )
    await session.commit()


def _intent_key(message_id: str, action: str, params: dict[str, Any]) -> str:
    raw = json.dumps(
        {"message_id": message_id, "action": action, "params": params},
        ensure_ascii=False,
        sort_keys=True,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _default_official_client(settings: Settings) -> ModerationActionClient | None:
    """惰性构建官方动作客户端（组合根 seam，见模块 docstring）。

    未配置官方凭据时返回 None，由调用方记录 SKIPPED 意图（不抛异常、
    不阻断影子链路）。官方 Adapter 仅在此函数体内被引用。
    """
    from app.actions.official_wiring import (
        build_official_action_client,
        official_client_configured,
    )

    if not official_client_configured(settings):
        return None
    return build_official_action_client(settings)
