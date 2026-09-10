"""T-305 按群 provider 路由测试：每群单一动作出口，跨通道不借用客户端。"""

from __future__ import annotations

import uuid

import pytest
from app.actions.orchestrator import ActionIntent, orchestrate_actions
from app.adapters.qq_official.contract import Sender, StandardMessage
from app.config import Settings
from app.core.contracts import ActionResult
from app.core.routing import (
    resolve_action_provider,
    upsert_group_route,
)
from app.db import SessionLocal
from app.moderation.decision import ModerationDecision
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class RecordingClient:
    """记录调用的假动作客户端（可代表官方或OneBot，结构一致）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def recall(self, g: str, m: str, *, actor: str = "system") -> ActionResult:
        self.calls.append(("recall", (g, m)))
        return ActionResult(action="recall", ok=True, attempts=1)

    async def mute(self, g: str, u: str, s: int, *, actor: str = "system") -> ActionResult:
        self.calls.append(("mute", (g, u, s)))
        return ActionResult(action="mute", ok=True, attempts=1)

    async def warn(self, g: str, r: str, t: str, *, actor: str = "system") -> ActionResult:
        self.calls.append(("warn", (g, r, t)))
        return ActionResult(action="warn", ok=True, attempts=1)


def _official_settings() -> Settings:
    return Settings(
        app_env="prod",
        admin_password="strong-admin-pass",
        qq_app_id="APP",
        qq_app_secret="SECRET",
        action_mode="OFFICIAL",
        _env_file=None,
    )


def _msg(group: str, member: str) -> StandardMessage:
    message_id = f"ROUTE_{uuid.uuid4().hex[:8]}"
    return StandardMessage(
        message_id=message_id,
        provider="onebot",
        external_group_id=group,
        external_user_id=member,
        group_openid=group,
        sender=Sender(member_openid=member, role="member"),
        text="违规测试内容",
    )


def _high_decision(msg: StandardMessage) -> ModerationDecision:
    return ModerationDecision(
        message_id=msg.message_id,
        group_openid=msg.group_openid,
        sender_member_openid=msg.sender.member_openid,
        provider=msg.provider,
        external_group_id=msg.external_group_id,
        external_user_id=msg.external_user_id,
        sender_role=msg.sender.role,
        verdict="violation_high",
        category="ad",
        confidence=0.95,
        recommended_actions=["recall"],
        reason="测试高置信违规",
    )


async def _enable_actions(
    session: AsyncSession, group_openid: str, provider: str = "onebot"
) -> None:
    """T-303 UX：OFFICIAL 模式测试需显式为测试群启用动作。"""
    from app.models import ProviderGroupSettings

    gs = await session.get(ProviderGroupSettings, (provider, group_openid))
    if gs is None:
        gs = ProviderGroupSettings(
            provider=provider, external_group_id=group_openid, action_enabled=True
        )
        session.add(gs)
    else:
        gs.action_enabled = True
    await session.commit()


@pytest.mark.asyncio
async def test_default_official_route_keeps_legacy_official_behavior() -> None:
    group = f"G_ROUTE_DEFAULT_{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        assert await resolve_action_provider(session, "qq_official", group) == "qq_official"


@pytest.mark.asyncio
async def test_unconfigured_onebot_route_fails_closed() -> None:
    group = f"3009{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        assert await resolve_action_provider(session, "onebot", group) is None


@pytest.mark.asyncio
async def test_route_rejects_unknown_provider() -> None:
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="非法 provider"):
            await upsert_group_route(
                session, f"G_ROUTE_BAD_{uuid.uuid4().hex[:6]}", action_provider="wechat"
            )


@pytest.mark.asyncio
async def test_route_rejects_cross_provider_without_verified_identity_mapping() -> None:
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="跨通道身份映射"):
            await upsert_group_route(
                session,
                f"G_ROUTE_CROSS_{uuid.uuid4().hex[:6]}",
                message_provider="onebot",
                action_provider="qq_official",
            )


@pytest.mark.asyncio
async def test_routed_onebot_group_never_borrows_official_client() -> None:
    """路由为 onebot 的群：官方客户端调用数必须为0，只记录SKIPPED意图。"""
    group = f"3000{uuid.uuid4().hex[:8]}"
    official = RecordingClient("official")
    msg = _msg(group, "200000001")
    async with SessionLocal() as session:
        await _enable_actions(session, group)
        await upsert_group_route(
            session, group, message_provider="onebot", action_provider="onebot"
        )
        assert await resolve_action_provider(session, "onebot", group) == "onebot"
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=official,  # type: ignore[arg-type]
            settings=_official_settings(),
        )

    assert official.calls == []  # 跨通道借用被禁止
    assert len(intents) == 1
    assert intents[0].status == "SKIPPED"
    assert intents[0].provider == "onebot"
    assert intents[0].external_group_id == group
    assert "ONEBOT_ACTIONS_ENABLED" in intents[0].reason


@pytest.mark.asyncio
async def test_official_mode_never_enables_injected_onebot_client() -> None:
    """T-305 不得复用 OFFICIAL 开关执行 OneBot 动作。"""
    group = f"3001{uuid.uuid4().hex[:8]}"
    official = RecordingClient("official")
    onebot = RecordingClient("onebot")
    msg = _msg(group, "200000002")
    async with SessionLocal() as session:
        await _enable_actions(session, group)
        await upsert_group_route(
            session, group, message_provider="onebot", action_provider="onebot"
        )
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=official,  # type: ignore[arg-type]
            settings=_official_settings(),
        )
        route_rows = (
            (
                await session.execute(
                    select(ActionIntent).where(ActionIntent.message_id == msg.message_id)
                )
            )
            .scalars()
            .all()
        )

    assert official.calls == []
    assert onebot.calls == []
    assert [i.status for i in intents] == ["SKIPPED"]
    assert all(i.provider == "onebot" for i in route_rows)
    assert all(i.external_group_id == group for i in route_rows)
    assert "ONEBOT_ACTIONS_ENABLED" in intents[0].reason


@pytest.mark.asyncio
async def test_same_external_group_id_retains_routes_but_only_latest_owner_executes() -> None:
    group = f"SAME_{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        await upsert_group_route(
            session, group, message_provider="qq_official", action_provider="qq_official"
        )
        await upsert_group_route(
            session, group, message_provider="onebot", action_provider="onebot"
        )
        assert await resolve_action_provider(session, "qq_official", group) is None
        assert await resolve_action_provider(session, "onebot", group) == "onebot"


@pytest.mark.asyncio
async def test_unrouted_group_keeps_official_behavior() -> None:
    """未配置路由的群保持既有官方行为（回归保护）。"""
    group = f"G_ROUTE_OFF_{uuid.uuid4().hex[:6]}"
    official = RecordingClient("official")
    msg = StandardMessage(
        message_id=f"ROUTE_OFF_{uuid.uuid4().hex[:8]}",
        group_openid=group,
        sender=Sender(member_openid="M_OFF", role="member"),
        text="违规测试内容",
    )
    async with SessionLocal() as session:
        await _enable_actions(session, group, provider="qq_official")
        intents = await orchestrate_actions(
            session,
            msg,
            _high_decision(msg),
            official_client=official,  # type: ignore[arg-type]
            settings=_official_settings(),
        )

    assert [call[0] for call in official.calls] == ["recall", "mute", "warn"]
    assert [i.status for i in intents] == ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
    assert intents[0].provider == "qq_official"
    assert intents[0].external_group_id == group
