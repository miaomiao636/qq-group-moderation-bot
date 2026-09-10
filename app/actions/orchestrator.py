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

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, cast

from sqlalchemy import DateTime, Integer, String, Text, and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.cases.service import record_violation
from app.config import Settings, get_settings
from app.core.contracts import ActionResult, ModerationActionClient, Provider, StandardMessage
from app.core.emergency_stop import emergency_stop_active
from app.core.routing import resolve_action_provider
from app.db import Base
from app.models import ActionLog, AdminAudit
from app.moderation.decision import ModerationDecision

ActionMode = Literal["SHADOW", "OFFICIAL"]
IntentStatus = Literal["PENDING", "EXECUTING", "SUCCEEDED", "FAILED", "UNKNOWN", "SKIPPED"]

MEMBER_ACTION_CHAIN_WAIT_SECONDS = 30.0


@dataclass
class _MemberActionChain:
    lock: asyncio.Lock
    users: int = 0


# OneBot's OS runtime lock permits only one live runtime per database. This
# loop-local lock orders that runtime's workers without holding SQLite over I/O.
# Entries are reference-counted (including waiters) and removed when idle.
_member_action_chains: dict[tuple[int, str, str, str], _MemberActionChain] = {}


@asynccontextmanager
async def _member_action_chain(msg: StandardMessage) -> AsyncIterator[bool]:
    key = (
        id(asyncio.get_running_loop()),
        msg.provider,
        msg.external_group_id,
        msg.external_user_id,
    )
    entry = _member_action_chains.setdefault(key, _MemberActionChain(asyncio.Lock()))
    entry.users += 1
    acquired = False
    try:
        try:
            await asyncio.wait_for(entry.lock.acquire(), timeout=MEMBER_ACTION_CHAIN_WAIT_SECONDS)
        except TimeoutError:
            yield False
        else:
            acquired = True
            yield True
    finally:
        if acquired:
            entry.lock.release()
        entry.users -= 1
        if entry.users == 0:
            del _member_action_chains[key]


# P1-11: 运行时急停（不重启即时生效）
_runtime_emergency_stop: bool = False


def set_runtime_emergency_stop(value: bool) -> None:
    """设置运行时急停状态。触发后下一次动作执行前立即阻断，不需要重启。"""
    global _runtime_emergency_stop
    _runtime_emergency_stop = value


def is_runtime_emergency_stop() -> bool:
    """读取运行时急停状态。读取失败时 fail-closed（返回 True）。"""
    return _runtime_emergency_stop


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
    onebot_client: ModerationActionClient | None = None,
    settings: Settings | None = None,
    actor: str = "system",
) -> list[ActionIntent]:
    """Persist and execute recall/mute/warn in guarded OFFICIAL mode.

    T-305：先按消息来源+群解析动作出口。``ACTION_MODE=OFFICIAL`` 下
    QQ官方与OneBot Adapter 各自需要显式客户端配置；OneBot 真实动作
    另需 ``ONEBOT_ACTIONS_ENABLED`` 独立开关（T-307），代码同步、
    服务重启或NapCat重连都不会自动开启。
    """
    settings = settings or get_settings()
    if settings.action_mode != "OFFICIAL":
        return []
    async with _member_action_chain(msg) as acquired:
        if not acquired:
            intent = await _record_skipped(
                session, msg, "recall", actor, "同成员动作链等待超时，仅记录并转人工处理"
            )
            session.add(
                AdminAudit(
                    operator=actor,
                    action="member_action_chain_timeout",
                    target_type="moderation_message",
                    target_id=msg.external_message_id,
                    detail_json=json.dumps(
                        {
                            "provider": msg.provider,
                            "external_group_id": msg.external_group_id,
                            "external_user_id": msg.external_user_id,
                            "external_message_id": msg.external_message_id,
                            "verdict": "record_only",
                            "reason": "member_action_chain_timeout",
                        },
                        sort_keys=True,
                    ),
                )
            )
            await session.commit()
            return [intent]
        return await _orchestrate_member_actions(
            session,
            msg,
            decision,
            official_client=official_client,
            onebot_client=onebot_client,
            settings=settings,
            actor=actor,
        )


async def _orchestrate_member_actions(
    session: AsyncSession,
    msg: StandardMessage,
    decision: ModerationDecision,
    *,
    official_client: ModerationActionClient | None,
    onebot_client: ModerationActionClient | None,
    settings: Settings,
    actor: str,
) -> list[ActionIntent]:
    """Hold one member's chain lock from strike allocation through external actions."""
    existing = await _existing_message_intents(session, msg)
    if existing:
        return existing
    # P1-11: 运行时急停即时生效（不重启），检查在配置急停之后、动作执行之前
    if (
        settings.emergency_stop
        or is_runtime_emergency_stop()
        or await emergency_stop_active(session)
    ):
        return [await _record_skipped(session, msg, "recall", actor, "急停开关开启，禁止外部动作")]
    if decision.verdict != "violation_high":
        return []
    if decision.is_protected_sender or msg.sender.role in ("owner", "admin"):
        return [await _record_skipped(session, msg, "recall", actor, "保护角色，编排层拦截")]
    if not decision.recommended_actions:
        return []

    async with AsyncSession(bind=session.bind) as control:
        correction = await _human_correction_reason(
            control,
            msg.provider,
            msg.external_group_id,
            msg.external_user_id,
            msg.external_message_id,
        )
    if correction:
        return [await _record_skipped(session, msg, "recall", actor, correction)]

    # 按群动作开关：禁用群不执行任何动作（安全默认值）
    from app.core.group_settings import is_action_enabled

    if not await is_action_enabled(session, msg.external_group_id, provider=msg.provider):
        return [await _record_skipped(session, msg, "recall", actor, "群动作已禁用（管理员设置）")]

    provider = await resolve_action_provider(session, msg.provider, msg.external_group_id)
    if provider is None:
        return [
            await _record_skipped(
                session,
                msg,
                "recall",
                actor,
                f"群 {msg.external_group_id} 未配置与消息来源 {msg.provider} 匹配的动作路由",
            )
        ]
    client: ModerationActionClient | None
    if provider == "qq_official":
        client = (
            official_client if official_client is not None else _default_official_client(settings)
        )
        if client is None:
            return [
                await _record_skipped(session, msg, "recall", actor, "QQ官方动作出口未配置客户端")
            ]
    elif provider == "onebot":
        if not settings.onebot_actions_enabled:
            return [
                await _record_skipped(
                    session,
                    msg,
                    "recall",
                    actor,
                    "OneBot真实动作开关未开启（ONEBOT_ACTIONS_ENABLED），仅记录不执行",
                )
            ]
        client = onebot_client if onebot_client is not None else _default_onebot_client(settings)
        if client is None:
            return [
                await _record_skipped(session, msg, "recall", actor, "OneBot动作出口未配置客户端")
            ]
    else:
        return [
            await _record_skipped(
                session,
                msg,
                "recall",
                actor,
                f"provider {provider} 无可用动作出口（fail-closed）",
            )
        ]

    outcome = await record_violation(session, msg, decision)
    intents: list[ActionIntent] = []
    for planned in outcome.planned_actions:
        if (
            provider == "onebot"
            and settings.onebot_action_stage == "recall_only"
            and planned.action != "recall"
        ):
            continue
        intent = await _create_intent(
            session, msg, planned.action, planned.params, actor, provider=provider
        )
        intents.append(intent)
        if intent.status != "PENDING":
            continue
        result = await _execute_intent(session, client, intent)
        if result is None:
            # Another worker owns this intent. Do not overwrite it or advance
            # this competing chain to a stronger action.
            break
        await _log_action_result(session, intent, result, actor=actor)
        if intent.status in ("UNKNOWN", "FAILED", "SKIPPED"):
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
            "external_message_id": intent.external_message_id,
            "external_group_id": intent.external_group_id,
            "external_user_id": intent.external_user_id,
            "target_member_openid": intent.target_member_openid,
            "reason": intent.reason,
        }
        for intent in intents
    ]


async def _existing_message_intents(
    session: AsyncSession, msg: StandardMessage
) -> list[ActionIntent]:
    neutral_identity = and_(
        ActionIntent.provider == msg.provider,
        ActionIntent.external_group_id == msg.external_group_id,
        ActionIntent.external_message_id == msg.external_message_id,
    )
    # expand/migrate 兼容：T-305前的官方意图只有旧 message_id。
    identity_filter = neutral_identity
    if msg.provider == "qq_official":
        legacy_official = and_(
            ActionIntent.external_group_id == "",
            ActionIntent.external_message_id == "",
            ActionIntent.message_id == msg.message_id,
        )
        identity_filter = or_(neutral_identity, legacy_official)
    return list(
        (
            await session.execute(
                select(ActionIntent).where(identity_filter).order_by(ActionIntent.id.asc())
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
    key = _intent_key(msg, action, {"reason": reason})
    existing = await session.scalar(select(ActionIntent).where(ActionIntent.idempotency_key == key))
    if existing is not None:
        return existing
    intent = ActionIntent(
        idempotency_key=key,
        action=action,
        status="SKIPPED",
        group_openid=msg.external_group_id,
        target_member_openid=msg.external_user_id,
        message_id=msg.message_id,
        provider=msg.provider,
        external_group_id=msg.external_group_id,
        external_user_id=msg.external_user_id,
        external_message_id=msg.external_message_id,
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
        raise ValueError(f"非法动作: {action}")
    key = _intent_key(msg, action, params)
    existing = await session.scalar(select(ActionIntent).where(ActionIntent.idempotency_key == key))
    if existing is not None:
        return existing
    intent = ActionIntent(
        idempotency_key=key,
        action=action,
        status="PENDING",
        group_openid=msg.external_group_id,
        target_member_openid=str(params.get("external_user_id") or ""),
        message_id=msg.message_id,
        provider=provider,
        external_group_id=msg.external_group_id,
        external_user_id=str(params.get("external_user_id") or msg.external_user_id),
        external_message_id=msg.external_message_id,
        params_json=json.dumps(params, ensure_ascii=False, sort_keys=True),
        actor=actor,
    )
    session.add(intent)
    await session.commit()
    return intent


async def _human_correction_reason(
    session: AsyncSession,
    provider: str,
    group_id: str,
    user_id: str,
    external_message_id: str,
) -> str:
    """Use exact neutral identity, never infer OneBot IDs from internal key text."""
    from app.cases.models import ViolationRecord
    from app.moderation.feedback import NEGATIVE_LABELS, FeedbackRecord
    from app.runtime.models import ShadowDecision

    revoked = await session.scalar(
        select(ViolationRecord.id)
        .where(
            ViolationRecord.provider == provider,
            ViolationRecord.external_group_id == group_id,
            ViolationRecord.external_user_id == user_id,
            # The strike service stores the parsed message's external ID, not
            # the shadow/inbox key (OneBot shadow keys include self_id).
            ViolationRecord.message_id == external_message_id,
            ViolationRecord.revoked.is_(True),
        )
        .limit(1)
    )
    if revoked is not None:
        return "本条违规已被人工撤销，未发送外部动作"
    latest_label = await session.scalar(
        select(FeedbackRecord.label)
        .join(
            ShadowDecision,
            and_(
                FeedbackRecord.message_id == ShadowDecision.message_id,
                FeedbackRecord.provider == ShadowDecision.provider,
                FeedbackRecord.external_group_id == ShadowDecision.external_group_id,
                FeedbackRecord.external_user_id == ShadowDecision.external_user_id,
            ),
        )
        .where(
            ShadowDecision.provider == provider,
            ShadowDecision.external_group_id == group_id,
            ShadowDecision.external_user_id == user_id,
            ShadowDecision.external_message_id == external_message_id,
        )
        # Match feedback truth selection: insertion order survives clock rollback.
        .order_by(FeedbackRecord.id.desc())
        .limit(1)
    )
    if latest_label in NEGATIVE_LABELS:
        return "最新人工反馈已确认为正常或误判，未发送外部动作"
    return ""


async def _execute_intent(
    session: AsyncSession,
    client: ModerationActionClient,
    intent: ActionIntent,
) -> ActionResult | None:
    claimed = await session.execute(
        update(ActionIntent)
        .where(
            ActionIntent.id == intent.id,
            ActionIntent.status == "PENDING",
        )
        .values(status="EXECUTING", updated_at=_utcnow())
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        await session.refresh(intent)
        return None
    await session.commit()
    params = json.loads(intent.params_json)
    from app.core.group_settings import is_action_enabled

    reason = ""
    async with AsyncSession(bind=session.bind) as control:
        if not await is_action_enabled(control, intent.external_group_id, provider=intent.provider):
            reason = "群动作已关闭，未发送外部动作"
        elif (
            await resolve_action_provider(
                control, cast(Provider, intent.provider), intent.external_group_id
            )
            != intent.provider
        ):
            reason = "动作出口已改变或有歧义，未发送外部动作"
        else:
            reason = await _human_correction_reason(
                control,
                intent.provider,
                intent.external_group_id,
                intent.external_user_id,
                intent.external_message_id,
            )
    if is_runtime_emergency_stop() or await emergency_stop_active(session):
        reason = "运行时急停，未发送外部动作"
    if reason:
        intent.status = "SKIPPED"
        intent.reason = reason
        result = ActionResult(action=intent.action, ok=False, err_message=intent.reason, attempts=0)
        intent.result_json = result.model_dump_json()
        await session.commit()
        return result
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
                str(
                    params.get("reply_to_external_message_id")
                    or intent.external_message_id
                    or intent.message_id
                ),
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


def _intent_key(msg: StandardMessage, action: str, params: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "provider": msg.provider,
            "external_group_id": msg.external_group_id,
            "external_message_id": msg.external_message_id,
            "action": action,
            "params": params,
        },
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


def _default_onebot_client(settings: Settings) -> ModerationActionClient | None:
    """惰性构建 OneBot 动作客户端（组合根 seam，与官方对称）。

    ``ONEBOT_ACTIONS_ENABLED`` 已在调用前检查；此处再校验 WS 配置齐全，
    未配置时返回 None，由调用方记录 SKIPPED 意图。
    """
    from app.actions.onebot_wiring import build_onebot_action_client, onebot_actions_configured

    if not onebot_actions_configured(settings):
        return None
    return build_onebot_action_client(settings)
