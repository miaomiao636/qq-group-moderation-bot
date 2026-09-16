"""全局白名单（负责人 2026-09-16）：命中且非严重类别 → 完全放行。

- 匹配：变体归一化（apply_variants：谐音/代称/大小写）+ 子串包含；
- 边界：仅豁免"广告/无信号"类别——诈骗/色情/暴力/刷屏不豁免（B-2 底线），
  由规则引擎层（app.moderation.rules）执行类别判断，本模块只提供词与匹配；
- 生效：运行时每条消息直读数据库（参照 emergency_stop 的跨进程模式，
  fresh 连接绕过调用方 WAL 快照），后台保存后下一条消息立即生效，无需重启；
- fail-closed：读取失败返回空集——绝不因故障放松任何判定。
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAudit, AllowlistTerm
from app.moderation.normalization import apply_variants

MAX_TERM_LENGTH = 64


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
    """每消息 fresh 读取启用中的白名单（跨进程立即生效；fail-closed）。"""
    try:
        async with AsyncSession(bind=session.bind) as reader:
            rows = (
                await reader.scalars(
                    select(AllowlistTerm.normalized).where(AllowlistTerm.enabled.is_(True))
                )
            ).all()
        return frozenset(str(row).strip() for row in rows if str(row).strip())
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
    await session.commit()
    await session.refresh(row)
    return row, True


async def set_term_enabled(
    session: AsyncSession, term_id: int, enabled: bool, *, operator: str
) -> AllowlistTerm:
    """启用/停用单条（立即生效；审计）。"""
    row = await session.get(AllowlistTerm, term_id)
    if row is None:
        raise ValueError("白名单词不存在")
    row.enabled = enabled
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="allowlist_enable" if enabled else "allowlist_disable",
            target_type="allowlist_term",
            target_id=row.normalized,
            detail_json=json.dumps({"term": row.term, "enabled": enabled}),
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
