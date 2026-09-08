"""按群显式的传输通道路由（T-305）。

架构约束（AGENTS #13 / D-019）：每个 ``message_provider +
external_group_id`` 只能有一个激活消息入口和一个自动处罚出口。
未显式配置的官方群保留历史默认；未配置的 OneBot 群必须默认拒绝，
不得把数字 ID 送到官方 API。

本模块属于审核核心：不得导入任何供应商 Adapter。
真实 NapCat 动作开关、按群影子/实时切换由 T-307 在本表基础上扩展。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.contracts import PROVIDERS, Provider
from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GroupProviderRoute(Base):
    """每群一条的传输通道路由：消息入口 provider + 自动动作出口 provider。"""

    __tablename__ = "group_provider_routes"

    message_provider: Mapped[str] = mapped_column(
        String(16), primary_key=True, default="qq_official", server_default="qq_official"
    )
    external_group_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    action_provider: Mapped[str] = mapped_column(
        String(16), default="qq_official", server_default="qq_official"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


async def resolve_action_provider(
    session: AsyncSession, message_provider: Provider, external_group_id: str
) -> Provider | None:
    """按消息来源和群身份解析出口；不可确定时 fail-closed。"""
    route = await session.get(GroupProviderRoute, (message_provider, external_group_id))
    if route is None:
        return "qq_official" if message_provider == "qq_official" else None
    provider = route.action_provider
    if provider not in PROVIDERS:
        return None
    # 当前没有可验证的跨通道身份映射；T-307 前禁止交叉出口。
    if provider != message_provider:
        return None
    return provider


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
    if action_provider != message_provider:
        raise ValueError("未建立可验证的跨通道身份映射，禁止配置交叉动作出口")
    route = await session.get(GroupProviderRoute, (message_provider, external_group_id))
    if route is None:
        route = GroupProviderRoute(
            message_provider=message_provider, external_group_id=external_group_id
        )
        session.add(route)
    route.action_provider = action_provider
    await session.commit()
    return route


async def list_group_routes(session: AsyncSession) -> list[GroupProviderRoute]:
    result = await session.execute(
        select(GroupProviderRoute).order_by(
            GroupProviderRoute.message_provider, GroupProviderRoute.external_group_id
        )
    )
    return list(result.scalars().all())
