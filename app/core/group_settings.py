"""按群管理设置：审核与动作开关（管理员可视化操作）。

- 无记录 = 默认审核启用、动作禁用（安全默认值）；
- 切OFFICIAL前必须显式为每个目标群开启 action_enabled。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contracts import PROVIDERS
from app.models import ProviderGroupSettings


async def resolve_group_identity(session: AsyncSession, group_id: str, provider: str = "") -> str:
    """An omitted provider is compatible only for an unambiguous persisted identity."""
    if not group_id or len(group_id) > 128:
        raise ValueError("群 ID 必须为 1–128 个字符")
    if provider:
        if provider not in PROVIDERS:
            raise ValueError("非法 provider；仅支持 onebot / qq_official")
        return provider
    from app.core.routing import GroupProviderRoute
    from app.runtime.models import ShadowDecision

    identities = set(
        (
            await session.scalars(
                select(ProviderGroupSettings.provider).where(
                    ProviderGroupSettings.external_group_id == group_id
                )
            )
        ).all()
    )
    identities.update(
        (
            await session.scalars(
                select(ShadowDecision.provider)
                .where(ShadowDecision.external_group_id == group_id)
                .distinct()
            )
        ).all()
    )
    identities.update(
        (
            await session.scalars(
                select(GroupProviderRoute.message_provider).where(
                    GroupProviderRoute.external_group_id == group_id
                )
            )
        ).all()
    )
    if len(identities) != 1:
        raise ValueError("群来源缺失或有多个消息来源，请明确选择 provider")
    return identities.pop()


async def is_moderation_enabled(session: AsyncSession, group_openid: str, *, provider: str) -> bool:
    """群审核是否启用。无记录=默认启用（向后兼容）。"""
    gs = await session.get(ProviderGroupSettings, (provider, group_openid), populate_existing=True)
    return bool(gs.moderation_enabled) if gs is not None else True


async def ambiguous_legacy_rule_scope(session: AsyncSession, group_id: str, provider: str) -> bool:
    """Legacy group rules lack provider: never share them across known identities."""
    from app.core.routing import GroupProviderRoute
    from app.runtime.models import ShadowDecision

    for model, provider_column, group_column in (
        (
            ProviderGroupSettings,
            ProviderGroupSettings.provider,
            ProviderGroupSettings.external_group_id,
        ),
        (
            GroupProviderRoute,
            GroupProviderRoute.message_provider,
            GroupProviderRoute.external_group_id,
        ),
        (ShadowDecision, ShadowDecision.provider, ShadowDecision.external_group_id),
    ):
        if await session.scalar(
            select(provider_column)
            .select_from(model)
            .where(group_column == group_id, provider_column != provider)
            .limit(1)
        ):
            return True
    return False


async def is_action_enabled(session: AsyncSession, group_openid: str, *, provider: str) -> bool:
    """群动作是否启用。无记录=默认禁用（安全默认值，切OFFICIAL前必须显式开启）。"""
    gs = await session.get(ProviderGroupSettings, (provider, group_openid), populate_existing=True)
    return bool(gs.action_enabled and gs.moderation_enabled) if gs is not None else False


async def get_or_create_group_settings(
    session: AsyncSession, group_openid: str, *, provider: str
) -> ProviderGroupSettings:
    """获取群设置；无则创建（默认审核启用、动作禁用）。"""
    provider = await resolve_group_identity(session, group_openid, provider)
    gs = await session.get(ProviderGroupSettings, (provider, group_openid), populate_existing=True)
    if gs is None:
        gs = ProviderGroupSettings(provider=provider, external_group_id=group_openid)
        session.add(gs)
        await session.flush()
    return gs
