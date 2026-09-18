"""全局白名单（负责人 2026-09-16）：命中且非严重类别 → 完全放行。

- 匹配：变体归一化（apply_variants：谐音/代称/大小写）+ 子串包含；
- 边界：仅豁免"广告/无信号"类别——诈骗/色情/暴力/刷屏不豁免（B-2 底线），
  由规则引擎层（app.moderation.rules）执行类别判断，本模块只提供词与匹配；
- 生效：运行时每条消息直读数据库（参照 emergency_stop 的跨进程模式，
  fresh 连接绕过调用方 WAL 快照），后台保存后下一条消息立即生效，无需重启；
- fail-closed：读取失败返回空集——绝不因故障放松任何判定。

成员白名单（负责人 2026-09-18，本模块下半部分）：
- 按 ``provider + external_user_id`` **精确匹配**（NapCat 主通道即数字 QQ 号）；
- 与关键词白名单**语义不同**：命中成员为全类别完全放行（负责人明确选择
  "不守 B-2 底线"），因此绝不复用关键词的变体归一化；
- 同样每消息直读、fail-closed、变更经 AdminAudit，并额外支持"整份文件导入/导出"。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAudit, AllowlistMember, AllowlistTerm
from app.moderation.allowlist_members_io import (
    InvalidLine,
    MemberListParse,
    ParsedMember,
    parse_member_list,
    validate_member_id,
)
from app.moderation.normalization import apply_variants

MAX_TERM_LENGTH = 64
# 成员白名单默认通道：NapCat/OneBot 主通道（数字 QQ 号）。官方通道是 openid，不适用。
DEFAULT_MEMBER_PROVIDER = "onebot"
# 全量同步的二次确认门槛：删除（停用）超过 5 条**且**超过当前启用数的 30%
MEMBER_SYNC_MIN_ABSOLUTE = 5
MEMBER_SYNC_RATIO = 0.30


def normalize_term(term: str) -> str:
    """白名单词的匹配形式（与消息文本同源归一化）。"""
    return apply_variants(term.strip())


def match_allowlist(text: str, normalized_terms: frozenset[str]) -> str | None:
    """归一化后子串匹配；返回命中的归一化词，未命中返回 None。"""
    if not text or not normalized_terms:
        return None
    variant = apply_variants(text)
    for term in normalized_terms:
        if term and term in variant:
            return term
    return None


async def load_allowlist_terms(session: AsyncSession) -> frozenset[str]:
    """每消息 fresh 读取启用中的白名单（跨进程立即生效；fail-closed）。

    R-115 W04：同一归一化词若存在多行（唯一约束上线前的历史残留），
    **全部启用才生效**——不静默取更宽松值；迁移已核查现库无重复。
    """
    try:
        async with AsyncSession(bind=session.bind) as reader:
            rows = (
                await reader.execute(select(AllowlistTerm.normalized, AllowlistTerm.enabled))
            ).all()
        effective: dict[str, bool] = {}
        for normalized, enabled in rows:
            key = str(normalized).strip()
            if not key:
                continue
            effective[key] = effective.get(key, True) and bool(enabled)
        return frozenset(key for key, ok in effective.items() if ok)
    except Exception:  # noqa: BLE001 — 读不到就不放行任何内容（fail-closed）
        return frozenset()


async def add_term(session: AsyncSession, raw: str, *, operator: str) -> tuple[AllowlistTerm, bool]:
    """新增（校验 + 归一化去重）。返回 (行, 是否新建)；等价词已存在时返回既有项。"""
    term = raw.strip()
    if not term:
        raise ValueError("白名单词不能为空")
    if len(term) > MAX_TERM_LENGTH:
        raise ValueError(f"白名单词不得超过 {MAX_TERM_LENGTH} 字符")
    normalized = normalize_term(term)
    if not normalized:
        raise ValueError("该词归一化后为空，无法用于匹配")
    existing = await session.scalar(
        select(AllowlistTerm).where(AllowlistTerm.normalized == normalized)
    )
    if existing is not None:
        return existing, False
    row = AllowlistTerm(term=term, normalized=normalized, enabled=True, created_by=operator[:64])
    session.add(row)
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="allowlist_add",
            target_type="allowlist_term",
            target_id=normalized,
            detail_json=json.dumps({"term": term}),
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # R-115 W04：并发等价词同时通过"先查"——由 normalized 唯一约束兜底。
        # 回滚后安全复用已有项（启停状态原样保留，不静默取更宽松值）。
        await session.rollback()
        existing = await session.scalar(
            select(AllowlistTerm).where(AllowlistTerm.normalized == normalized)
        )
        if existing is None:
            raise
        return existing, False
    await session.refresh(row)
    return row, True


async def set_term_enabled(
    session: AsyncSession, term_id: int, enabled: bool, *, operator: str
) -> AllowlistTerm:
    """启用/停用单条（幂等 set —— 重复同目标不改变状态；立即生效；审计含实际变更）。"""
    row = await session.get(AllowlistTerm, term_id)
    if row is None:
        raise ValueError("白名单词不存在")
    before = bool(row.enabled)
    row.enabled = enabled
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="allowlist_enable" if enabled else "allowlist_disable",
            target_type="allowlist_term",
            target_id=row.normalized,
            detail_json=json.dumps(
                {
                    "term": row.term,
                    "enabled": enabled,
                    "before": before,
                    "changed": before != enabled,
                }
            ),
        )
    )
    await session.commit()
    return row


async def delete_term(session: AsyncSession, term_id: int, *, operator: str) -> str:
    """删除单条（立即生效；审计）。返回被删词原文。"""
    row = await session.get(AllowlistTerm, term_id)
    if row is None:
        raise ValueError("白名单词不存在")
    term = row.term
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="allowlist_delete",
            target_type="allowlist_term",
            target_id=row.normalized,
            detail_json=json.dumps({"term": term}),
        )
    )
    await session.delete(row)
    await session.commit()
    return term


# ---------------- 成员白名单（按 QQ 号；负责人 2026-09-18） ----------------


def match_allowlist_member(
    provider: str, external_user_id: str, members: frozenset[tuple[str, str]]
) -> str | None:
    """精确匹配 ``(provider, QQ号)``；命中返回 QQ 号，未命中返回 None。

    绝不做归一化：QQ 号是精确身份，与关键词白名单的谐音/大小写匹配语义不同。
    provider 参与匹配，官方通道 openid 不会误命中 QQ 号白名单。
    """
    user_id = str(external_user_id).strip()
    if not user_id or not members:
        return None
    return user_id if (str(provider), user_id) in members else None


async def load_allowlist_members(session: AsyncSession) -> frozenset[tuple[str, str]]:
    """每消息 fresh 读取启用中的成员白名单（跨进程立即生效；fail-closed 空集）。

    同一 ``(provider, QQ号)`` 若存在多行（唯一约束上线前的历史残留），
    **全部启用才生效**——不静默取更宽松值。
    """
    try:
        async with AsyncSession(bind=session.bind) as reader:
            rows = (
                await reader.execute(
                    select(
                        AllowlistMember.provider,
                        AllowlistMember.external_user_id,
                        AllowlistMember.enabled,
                    )
                )
            ).all()
        effective: dict[tuple[str, str], bool] = {}
        for provider, user_id, enabled in rows:
            key = (str(provider).strip(), str(user_id).strip())
            if not key[0] or not key[1]:
                continue
            effective[key] = effective.get(key, True) and bool(enabled)
        return frozenset(key for key, ok in effective.items() if ok)
    except Exception:  # noqa: BLE001 — 读不到就不放行任何成员（fail-closed）
        return frozenset()


async def list_members(
    session: AsyncSession, *, provider: str = DEFAULT_MEMBER_PROVIDER
) -> Sequence[AllowlistMember]:
    """列出某通道下的全部成员白名单（含已停用，供后台展示与同步对账）。"""
    return (
        await session.scalars(
            select(AllowlistMember)
            .where(AllowlistMember.provider == provider)
            .order_by(AllowlistMember.id)
        )
    ).all()


async def add_member(
    session: AsyncSession,
    raw_id: str,
    *,
    operator: str,
    provider: str = DEFAULT_MEMBER_PROVIDER,
    note: str = "",
) -> tuple[AllowlistMember, bool]:
    """新增成员白名单（校验 + 精确去重）。返回 (行, 是否新建)；已存在时原样返回。"""
    user_id = validate_member_id(raw_id)
    existing = await session.scalar(
        select(AllowlistMember).where(
            AllowlistMember.provider == provider,
            AllowlistMember.external_user_id == user_id,
        )
    )
    if existing is not None:
        return existing, False
    row = AllowlistMember(
        provider=provider,
        external_user_id=user_id,
        note=note.strip()[:64],
        enabled=True,
        created_by=operator[:64],
    )
    session.add(row)
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="allowlist_member_add",
            target_type="allowlist_member",
            target_id=f"{provider}:{user_id}",
            detail_json=json.dumps(
                {"provider": provider, "user_id": user_id, "note": note}, ensure_ascii=False
            ),
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        # 并发写入同一 QQ 号：由唯一约束兜底，回滚后安全复用已有项
        await session.rollback()
        existing = await session.scalar(
            select(AllowlistMember).where(
                AllowlistMember.provider == provider,
                AllowlistMember.external_user_id == user_id,
            )
        )
        if existing is None:
            raise
        return existing, False
    await session.refresh(row)
    return row, True


async def set_member_enabled(
    session: AsyncSession, member_id: int, enabled: bool, *, operator: str
) -> AllowlistMember:
    """启用/停用单条（幂等 set；立即生效；审计含实际变更）。"""
    row = await session.get(AllowlistMember, member_id)
    if row is None:
        raise ValueError("成员白名单不存在")
    before = bool(row.enabled)
    row.enabled = enabled
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="allowlist_member_enable" if enabled else "allowlist_member_disable",
            target_type="allowlist_member",
            target_id=f"{row.provider}:{row.external_user_id}",
            detail_json=json.dumps(
                {
                    "user_id": row.external_user_id,
                    "enabled": enabled,
                    "before": before,
                    "changed": before != enabled,
                },
                ensure_ascii=False,
            ),
        )
    )
    await session.commit()
    return row


async def delete_member(session: AsyncSession, member_id: int, *, operator: str) -> str:
    """删除单条（立即生效；审计）。返回被删 QQ 号。"""
    row = await session.get(AllowlistMember, member_id)
    if row is None:
        raise ValueError("成员白名单不存在")
    user_id = row.external_user_id
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="allowlist_member_delete",
            target_type="allowlist_member",
            target_id=f"{row.provider}:{user_id}",
            detail_json=json.dumps({"user_id": user_id}, ensure_ascii=False),
        )
    )
    await session.delete(row)
    await session.commit()
    return user_id


@dataclass(frozen=True)
class MemberSyncPlan:
    """整份文件全量同步计划（预览对象；构造过程不写库）。"""

    provider: str
    to_add: tuple[ParsedMember, ...]
    to_enable: tuple[tuple[int, str], ...]
    to_disable: tuple[tuple[int, str], ...]
    to_update_note: tuple[tuple[int, str, str], ...]
    unchanged: int
    enabled_before: int
    total_valid: int
    invalid: tuple[InvalidLine, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.to_add or self.to_enable or self.to_disable or self.to_update_note)

    @property
    def needs_confirm(self) -> bool:
        """停用数超过绝对下限**且**超过当前启用数的比例 → 必须二次确认。

        防止"误传残缺文件/误删一大段"一次性清掉白名单。
        """
        disabled = len(self.to_disable)
        if disabled <= MEMBER_SYNC_MIN_ABSOLUTE:
            return False
        return disabled > self.enabled_before * MEMBER_SYNC_RATIO


async def plan_member_import(
    session: AsyncSession, text: str, *, provider: str = DEFAULT_MEMBER_PROVIDER
) -> MemberSyncPlan:
    """解析文件并生成同步计划；**空列表直接拒绝**（不返回计划）。

    语义：文件是完整列表（全量替换）。文件里没有而库中仍启用的条目 → 停用
    （停用而非删除：保留备注与创建信息，便于回滚与重新启用）。
    """
    parsed: MemberListParse = parse_member_list(text)
    if not parsed.valid:
        raise ValueError("文件中没有有效的QQ号，已拒绝导入（防止误传空文件清空白名单）")
    rows = await list_members(session, provider=provider)
    by_id = {row.external_user_id: row for row in rows}
    file_ids = {member.user_id for member in parsed.valid}

    to_add: list[ParsedMember] = []
    to_enable: list[tuple[int, str]] = []
    to_update_note: list[tuple[int, str, str]] = []
    unchanged = 0
    for member in parsed.valid:
        row = by_id.get(member.user_id)
        if row is None:
            to_add.append(member)
            continue
        changed = False
        if not row.enabled:
            to_enable.append((row.id, row.external_user_id))
            changed = True
        if member.note and member.note != row.note:
            to_update_note.append((row.id, row.external_user_id, member.note))
            changed = True
        if not changed:
            unchanged += 1

    to_disable = [
        (row.id, row.external_user_id)
        for row in rows
        if row.enabled and row.external_user_id not in file_ids
    ]
    return MemberSyncPlan(
        provider=provider,
        to_add=tuple(to_add),
        to_enable=tuple(to_enable),
        to_disable=tuple(to_disable),
        to_update_note=tuple(to_update_note),
        unchanged=unchanged,
        enabled_before=sum(1 for row in rows if row.enabled),
        total_valid=len(parsed.valid),
        invalid=parsed.invalid,
    )


async def apply_member_import(
    session: AsyncSession, plan: MemberSyncPlan, *, operator: str
) -> None:
    """执行全量同步（新增/启用/停用/改备注）并审计；单事务提交，失败整体回滚。"""
    for member in plan.to_add:
        session.add(
            AllowlistMember(
                provider=plan.provider,
                external_user_id=member.user_id,
                note=member.note,
                enabled=True,
                created_by=operator[:64],
            )
        )
    for member_id, _user_id in plan.to_enable:
        row = await session.get(AllowlistMember, member_id)
        if row is not None:
            row.enabled = True
    for member_id, _user_id in plan.to_disable:
        row = await session.get(AllowlistMember, member_id)
        if row is not None:
            row.enabled = False
    for member_id, _user_id, note in plan.to_update_note:
        row = await session.get(AllowlistMember, member_id)
        if row is not None:
            row.note = note
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="allowlist_members_import",
            target_type="allowlist_member",
            target_id=plan.provider,
            detail_json=json.dumps(
                {
                    "provider": plan.provider,
                    "valid": plan.total_valid,
                    "added": [m.user_id for m in plan.to_add],
                    "enabled": [uid for _id, uid in plan.to_enable],
                    "disabled": [uid for _id, uid in plan.to_disable],
                    "note_updated": [uid for _id, uid, _n in plan.to_update_note],
                    "unchanged": plan.unchanged,
                    "invalid_lines": len(plan.invalid),
                    "enabled_before": plan.enabled_before,
                },
                ensure_ascii=False,
            ),
        )
    )
    await session.commit()
