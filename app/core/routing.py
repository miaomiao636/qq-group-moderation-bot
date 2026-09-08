"""按群显式的传输通道路由（T-305）。

架构约束（AGENTS #13 / D-019）：每个群默认只能有**一个**激活消息入口和
**一个**自动处罚出口。本模块提供每群一条路由记录（主键即 external_group_id），
天然保证单一出口；未显式配置路由的群回退到 ``qq_official``（兼容既有官方
链路与全部既有测试）。

本模块属于审核核心：不得导入任何供应商 Adapter。
真实 NapCat 动作开关、按群影子/实时切换由 T-307 在本表基础上扩展。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.contracts import PROVIDERS
from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GroupProviderRoute(Base):
    """每群一条的传输通道路由：消息入口 provider + 自动动作出口 provider。"""

    __tablename__ = "group_provider_routes"

    external_group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_provider: Mapped[str] = mapped_column(
        String(16), default="qq_official", server_default="qq_official"
    )
    action_provider: Mapped[str] = mapped_column(
        String(16), default="qq_official", server_default="qq_official"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


async def resolve_action_provider(session: AsyncSession, external_group_id: str) -> str:
    """解析群的动作出口 provider；无路由或非法值时安全回退 ``qq_official``。"""
    route = await session.get(GroupProviderRoute, external_group_id)
    if route is None:
        return "qq_official"
    provider = route.action_provider
    return provider if provider in PROVIDERS else "qq_official"


async def upsert_group_route(
    session: AsyncSession,
    external_group_id: str,
    *,
    message_provider: str = "qq_official",
    action_provider: str = "qq_official",
) -> GroupProviderRoute:
    """显式设置群路由（管理后台/运维入口）；非法 provider 拒绝。"""
    for name, value in (
        ("message_provider", message_provider),
        ("action_provider", action_provider),
    ):
        if value not in PROVIDERS:
            raise ValueError(f"非法 provider {value!r}（{name}），允许值: {PROVIDERS}")
    route = await session.get(GroupProviderRoute, external_group_id)
    if route is None:
        route = GroupProviderRoute(external_group_id=external_group_id)
        session.add(route)
    route.message_provider = message_provider
    route.action_provider = action_provider
    await session.commit()
    return route


async def list_group_routes(session: AsyncSession) -> list[GroupProviderRoute]:
    result = await session.execute(
        select(GroupProviderRoute).order_by(GroupProviderRoute.external_group_id)
    )
    return list(result.scalars().all())
