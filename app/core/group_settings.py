"""按群管理设置：审核与动作开关（管理员可视化操作）。

- 无记录 = 默认审核启用、动作禁用（安全默认值）；
- 切OFFICIAL前必须显式为每个目标群开启 action_enabled。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GroupSettings


async def is_moderation_enabled(session: AsyncSession, group_openid: str) -> bool:
    """群审核是否启用。无记录=默认启用（向后兼容）。"""
    gs = await session.get(GroupSettings, group_openid)
    return bool(gs.moderation_enabled) if gs is not None else True


async def is_action_enabled(session: AsyncSession, group_openid: str) -> bool:
    """群动作是否启用。无记录=默认禁用（安全默认值，切OFFICIAL前必须显式开启）。"""
    gs = await session.get(GroupSettings, group_openid)
    return bool(gs.action_enabled) if gs is not None else False


async def get_or_create_group_settings(session: AsyncSession, group_openid: str) -> GroupSettings:
    """获取群设置；无则创建（默认审核启用、动作禁用）。"""
    gs = await session.get(GroupSettings, group_openid)
    if gs is None:
        gs = GroupSettings(group_openid=group_openid)
        session.add(gs)
        await session.commit()
    return gs
