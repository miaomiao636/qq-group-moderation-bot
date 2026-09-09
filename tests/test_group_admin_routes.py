"""群管理面板"动作"开关自动补齐同通道路由测试（T-307 方案A）。"""

from __future__ import annotations

import uuid

import pytest
from app.core.routing import GroupProviderRoute, resolve_action_provider
from app.db import SessionLocal
from app.runtime.models import ShadowDecision
from app.web.routes import _ensure_action_routes
from sqlalchemy import select


async def _add_shadow(session, group: str, provider: str = "onebot") -> None:
    mid = f"k_{uuid.uuid4().hex[:6]}"
    session.add(
        ShadowDecision(
            message_id=mid,
            external_message_id=mid,
            group_openid=group,
            member_openid="u1",
            provider=provider,
            external_group_id=group,
            external_user_id="u1",
            verdict="record_only",
            kind="text",
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_enable_action_creates_same_provider_route() -> None:
    group = f"G{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        await _add_shadow(session, group, "onebot")
        routed = await _ensure_action_routes(session, group)
    assert routed == ["onebot"]
    async with SessionLocal() as session:
        assert await resolve_action_provider(session, "onebot", group) == "onebot"


@pytest.mark.asyncio
async def test_enable_action_no_messages_creates_no_route() -> None:
    group = f"G{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        routed = await _ensure_action_routes(session, group)
    assert routed == []
    async with SessionLocal() as session:
        assert await resolve_action_provider(session, "onebot", group) is None


@pytest.mark.asyncio
async def test_enable_action_idempotent_single_route() -> None:
    group = f"G{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        await _add_shadow(session, group, "onebot")
        await _ensure_action_routes(session, group)
        await _ensure_action_routes(session, group)
        rows = (
            (
                await session.execute(
                    select(GroupProviderRoute).where(GroupProviderRoute.external_group_id == group)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_disable_action_does_not_delete_route() -> None:
    """动作关闭不删路由：路由是拓扑信息，动作开关由 action_enabled 守门。"""
    group = f"G{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        await _add_shadow(session, group, "onebot")
        await _ensure_action_routes(session, group)
    # 模拟面板取消勾选动作：_ensure_action_routes 不会被调用，路由自然保留
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(GroupProviderRoute).where(GroupProviderRoute.external_group_id == group)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    async with SessionLocal() as session:
        assert await resolve_action_provider(session, "onebot", group) == "onebot"


@pytest.mark.asyncio
async def test_multi_provider_group_gets_route_per_provider() -> None:
    """同一群若被多通道见过消息，各补齐同通道路由（跨通道仍被禁止）。"""
    group = f"G{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        await _add_shadow(session, group, "onebot")
        await _add_shadow(session, group, "qq_official")
        routed = await _ensure_action_routes(session, group)
    assert set(routed) == {"onebot", "qq_official"}
    async with SessionLocal() as session:
        assert await resolve_action_provider(session, "onebot", group) == "onebot"
        assert await resolve_action_provider(session, "qq_official", group) == "qq_official"
