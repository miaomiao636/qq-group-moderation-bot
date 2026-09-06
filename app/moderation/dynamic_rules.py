"""Database-backed dynamic moderation rules.

Rules are versioned data, not executable code. Runtime uses immutable active
snapshots, while drafts can be edited safely in the admin UI.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.qq_official.contract import StandardMessage
from app.db import Base
from app.moderation.decision import Category, ModerationDecision, RuleHit, Verdict
from app.moderation.extract import extract_signals
from app.moderation.normalization import apply_variants

RuleScope = Literal["global", "group"]
RuleItemType = Literal[
    "keyword",
    "phrase_combo",
    "domain",
    "share_source",
    "qr_payload",
    "media_hash",
    "contact_combo",
    "behavior_threshold",
]
RuleStatus = Literal["DRAFT", "ACTIVE", "ARCHIVED"]

ALLOWED_CATEGORIES = {"ad", "fraud", "porn", "violence", "flood", "other", "allow"}
ALLOWED_ITEM_TYPES = {
    "keyword",
    "phrase_combo",
    "domain",
    "share_source",
    "qr_payload",
    "media_hash",
    "contact_combo",
    "behavior_threshold",
}
FORBIDDEN_PATTERN_MARKERS = (
    "__",
    "import(",
    "eval(",
    "exec(",
    "subprocess",
    "os.",
    "select ",
    "insert ",
    "update ",
    "delete ",
    "drop ",
    ".*",
    "(?",
    "[",
    "]",
    "{",
    "}",
    "|",
    "\\",
)
HIGH_THRESHOLD = 0.90
HIGH_ACTIONS = ["recall", "mute", "warn"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RuleSet(Base):
    """A global or group-specific rule set."""

    __tablename__ = "rule_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)
    scope_key: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RuleVersion(Base):
    """Immutable collection of rule items."""

    __tablename__ = "rule_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("rule_sets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), index=True, default="DRAFT")
    description: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuleItem(Base):
    """One structured rule item inside a version."""

    __tablename__ = "rule_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("rule_versions.id"), index=True)
    item_type: Mapped[str] = mapped_column(String(32))
    pattern: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(24), default="ad")
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RuleAudit(Base):
    """Audit trail for rule draft, publish, and rollback operations."""

    __tablename__ = "rule_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("rule_sets.id"), index=True)
    version_id: Mapped[int | None] = mapped_column(ForeignKey("rule_versions.id"), nullable=True)
    operator: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


@dataclass(frozen=True)
class RuntimeRuleItem:
    id: int
    item_type: RuleItemType
    pattern: str
    category: str
    weight: float
    enabled: bool = True
    description: str = ""


@dataclass(frozen=True)
class RuleSnapshot:
    version_id: int
    scope_key: str
    items: tuple[RuntimeRuleItem, ...]
    version_ids: tuple[int, ...] = ()


_CACHE: dict[str, tuple[float, RuleSnapshot]] = {}


async def create_rule_draft(
    session: AsyncSession,
    *,
    scope: RuleScope,
    scope_key: str,
    name: str,
    operator: str = "system",
) -> RuleVersion:
    rule_set = await _get_or_create_rule_set(session, scope, scope_key, name)
    next_version = (
        await session.scalar(
            select(func.max(RuleVersion.version)).where(RuleVersion.rule_set_id == rule_set.id)
        )
        or 0
    ) + 1
    version = RuleVersion(
        rule_set_id=rule_set.id,
        version=next_version,
        status="DRAFT",
        description=name[:255],
    )
    session.add(version)
    await session.flush()
    _add_rule_audit(session, rule_set.id, version.id, operator, "create_draft", {"name": name})
    await session.commit()
    return version


async def add_rule_item(
    session: AsyncSession,
    version_id: int,
    *,
    item_type: str,
    pattern: str,
    category: str,
    weight: float,
    description: str = "",
    enabled: bool = True,
    operator: str = "system",
) -> RuleItem:
    item_type_l, pattern_clean, category_l, weight_f = validate_rule_item(
        item_type, pattern, category, weight
    )
    version = await session.get(RuleVersion, version_id)
    if version is None:
        raise ValueError("规则版本不存在")
    if version.status != "DRAFT":
        raise ValueError("只能编辑草稿规则版本")
    item = RuleItem(
        version_id=version_id,
        item_type=item_type_l,
        pattern=pattern_clean,
        category=category_l,
        weight=weight_f,
        enabled=enabled,
        description=description[:255],
    )
    session.add(item)
    await session.flush()
    _add_rule_audit(
        session,
        version.rule_set_id,
        version.id,
        operator,
        "add_item",
        {"item_type": item_type_l, "category": category_l, "weight": weight_f},
    )
    await session.commit()
    _CACHE.clear()
    return item


async def publish_rule_version(
    session: AsyncSession, version_id: int, *, operator: str
) -> RuleVersion:
    version = await session.get(RuleVersion, version_id)
    if version is None:
        raise ValueError("规则版本不存在")
    if version.status != "DRAFT":
        raise ValueError("只能发布草稿规则版本")
    active_rows = (
        await session.execute(
            select(RuleVersion).where(
                RuleVersion.rule_set_id == version.rule_set_id,
                RuleVersion.status == "ACTIVE",
            )
        )
    ).scalars()
    for active in active_rows:
        active.status = "ARCHIVED"
    version.status = "ACTIVE"
    version.published_at = _utcnow()
    _add_rule_audit(session, version.rule_set_id, version.id, operator, "publish", {})
    await session.commit()
    _CACHE.clear()
    return version


async def rollback_to_version(
    session: AsyncSession, version_id: int, *, operator: str
) -> RuleVersion:
    version = await session.get(RuleVersion, version_id)
    if version is None:
        raise ValueError("规则版本不存在")
    active_rows = (
        await session.execute(
            select(RuleVersion).where(
                RuleVersion.rule_set_id == version.rule_set_id,
                RuleVersion.status == "ACTIVE",
            )
        )
    ).scalars()
    for active in active_rows:
        if active.id != version.id:
            active.status = "ARCHIVED"
    version.status = "ACTIVE"
    version.published_at = _utcnow()
    _add_rule_audit(session, version.rule_set_id, version.id, operator, "rollback", {})
    await session.commit()
    _CACHE.clear()
    return version


async def load_active_snapshot(session: AsyncSession, group_openid: str | None) -> RuleSnapshot:
    await ensure_default_rules(session)
    scope_keys = ["*"]
    if group_openid:
        scope_keys.append(group_openid)
    rows = (
        await session.execute(
            select(RuleVersion, RuleSet)
            .join(RuleSet, RuleSet.id == RuleVersion.rule_set_id)
            .where(RuleVersion.status == "ACTIVE", RuleSet.scope_key.in_(scope_keys))
            .order_by(RuleSet.scope.asc(), RuleVersion.id.asc())
        )
    ).all()
    versions = [version for version, _rule_set in rows]
    if not versions:
        return RuleSnapshot(version_id=0, scope_key=group_openid or "*", items=(), version_ids=())
    version_ids = tuple(version.id for version in versions)
    item_rows = (
        await session.execute(
            select(RuleItem)
            .where(RuleItem.version_id.in_(version_ids), RuleItem.enabled.is_(True))
            .order_by(RuleItem.id.asc())
        )
    ).scalars()
    items = tuple(
        RuntimeRuleItem(
            id=item.id,
            item_type=item.item_type,  # type: ignore[arg-type]
            pattern=item.pattern,
            category=item.category,
            weight=item.weight,
            enabled=item.enabled,
            description=item.description,
        )
        for item in item_rows
    )
    return RuleSnapshot(
        version_id=version_ids[-1],
        scope_key=group_openid or "*",
        items=items,
        version_ids=version_ids,
    )


async def load_cached_active_snapshot(
    session: AsyncSession,
    group_openid: str | None,
    *,
    ttl_seconds: float = 5.0,
    now: float | None = None,
) -> RuleSnapshot:
    now = time.monotonic() if now is None else now
    key = group_openid or "*"
    cached = _CACHE.get(key)
    if cached and now - cached[0] <= ttl_seconds:
        return cached[1]
    snapshot = await load_active_snapshot(session, group_openid)
    _CACHE[key] = (now, snapshot)
    return snapshot


def validate_rule_item(
    item_type: str, pattern: str, category: str, weight: float
) -> tuple[RuleItemType, str, str, float]:
    item_type_l = item_type.strip().lower()
    category_l = category.strip().lower()
    pattern_clean = pattern.strip()
    if item_type_l not in ALLOWED_ITEM_TYPES:
        raise ValueError("不支持的规则类型")
    if category_l not in ALLOWED_CATEGORIES:
        raise ValueError("不支持的规则类别")
    if not pattern_clean:
        raise ValueError("规则内容不能为空")
    if len(pattern_clean) > 200:
        raise ValueError("规则内容过长")
    if not 0.0 <= float(weight) <= 1.0:
        raise ValueError("规则权重必须在0到1之间")
    lowered = pattern_clean.lower()
    if any(marker in lowered for marker in FORBIDDEN_PATTERN_MARKERS):
        raise ValueError("规则内容只能是安全结构化文本，不能包含代码、SQL或任意正则")
    return item_type_l, pattern_clean, category_l, float(weight)  # type: ignore[return-value]


class DynamicRuleEngine:
    """Evaluate one immutable dynamic rule snapshot."""

    def __init__(self, snapshot: RuleSnapshot) -> None:
        self.snapshot = snapshot

    def evaluate(self, msg: StandardMessage) -> ModerationDecision:
        text = apply_variants(_message_text(msg))
        hits: list[RuleHit] = []
        total = 0.0
        category: Category = None
        allow_hit = False
        block_hit = False

        for item in self.snapshot.items:
            if not item.enabled or not _matches(item, msg, text):
                continue
            if item.category == "allow":
                allow_hit = True
                hits.append(
                    RuleHit(
                        rule_id=f"DR_ALLOW_{item.id}",
                        rule_name=item.item_type,
                        category="other",
                        confidence_delta=0.0,
                        evidence_masked="动态允许规则命中",
                    )
                )
                continue
            block_hit = True
            total = min(total + item.weight, 1.0)
            category = category or _category_or_other(item.category)
            hits.append(
                RuleHit(
                    rule_id=f"DR_{item.id}",
                    rule_name=item.item_type,
                    category=_category_or_other(item.category),
                    confidence_delta=item.weight,
                    evidence_masked=f"动态规则命中:{item.item_type}",
                )
            )

        protected = msg.sender.role in ("owner", "admin")
        if protected and hits:
            return _decision(
                msg, "record_only", category, min(total, 0.85), hits, "保护角色命中动态规则，仅记录"
            )
        if allow_hit and block_hit:
            return _decision(
                msg,
                "record_only",
                category,
                min(total, 0.85),
                hits,
                "允许规则与禁止规则冲突，转人工复核",
            )
        if allow_hit:
            return _decision(msg, "allow", None, 0.0, hits, "动态允许规则命中")
        if total >= HIGH_THRESHOLD:
            return _decision(msg, "violation_high", category, total, hits, "动态规则达到高置信阈值")
        if hits:
            return _decision(
                msg, "record_only", category, total, hits, "动态规则命中但未达高置信阈值"
            )
        return _decision(msg, "allow", None, 0.0, [], "未命中动态规则")


async def ensure_default_rules(session: AsyncSession) -> RuleVersion:
    existing = await _active_global_version(session)
    if existing is not None:
        return existing
    rule_set = await _get_or_create_rule_set(session, "global", "*", "系统默认规则")
    version = RuleVersion(
        rule_set_id=rule_set.id,
        version=1,
        status="ACTIVE",
        description="系统默认结构化规则",
        published_at=_utcnow(),
    )
    session.add(version)
    await session.flush()
    default_items = [
        ("share_source", "万能校园墙", "allow", 0.0, "允许来源"),
        ("keyword", "色情", "porn", 0.95, "严重违规词"),
        ("keyword", "裸聊", "porn", 0.95, "严重违规词"),
        ("keyword", "赌球", "fraud", 0.95, "严重违规词"),
        ("keyword", "网赌", "fraud", 0.95, "严重违规词"),
        ("keyword", "毒品", "violence", 0.95, "严重违规词"),
        ("keyword", "枪支", "violence", 0.95, "严重违规词"),
    ]
    for item_type, pattern, category, weight, description in default_items:
        session.add(
            RuleItem(
                version_id=version.id,
                item_type=item_type,
                pattern=pattern,
                category=category,
                weight=weight,
                description=description,
            )
        )
    _add_rule_audit(session, rule_set.id, version.id, "system", "seed_default", {})
    await session.commit()
    _CACHE.clear()
    return version


async def list_rule_versions(session: AsyncSession) -> list[RuleVersion]:
    return list(
        (
            await session.execute(
                select(RuleVersion).order_by(RuleVersion.created_at.desc(), RuleVersion.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def _active_global_version(session: AsyncSession) -> RuleVersion | None:
    version: RuleVersion | None = await session.scalar(
        select(RuleVersion)
        .join(RuleSet, RuleSet.id == RuleVersion.rule_set_id)
        .where(
            RuleSet.scope == "global",
            RuleSet.scope_key == "*",
            RuleVersion.status == "ACTIVE",
        )
    )
    return version


async def _get_or_create_rule_set(
    session: AsyncSession, scope: RuleScope, scope_key: str, name: str
) -> RuleSet:
    if scope == "global":
        scope_key = "*"
    scope_key = scope_key.strip() or "*"
    rule_set = await session.scalar(
        select(RuleSet).where(RuleSet.scope == scope, RuleSet.scope_key == scope_key)
    )
    if rule_set is not None:
        return rule_set
    rule_set = RuleSet(scope=scope, scope_key=scope_key, name=name[:128])
    session.add(rule_set)
    await session.flush()
    return rule_set


def _add_rule_audit(
    session: AsyncSession,
    rule_set_id: int,
    version_id: int | None,
    operator: str,
    action: str,
    details: dict[str, object],
) -> None:
    session.add(
        RuleAudit(
            rule_set_id=rule_set_id,
            version_id=version_id,
            operator=operator[:64],
            action=action,
            detail_json=json.dumps(details, ensure_ascii=False),
        )
    )


def _matches(item: RuntimeRuleItem, msg: StandardMessage, normalized_text: str) -> bool:
    pattern = apply_variants(item.pattern)
    if item.item_type in ("keyword", "domain", "qr_payload", "media_hash"):
        return pattern in normalized_text
    if item.item_type == "phrase_combo":
        parts = [p.strip() for p in pattern.replace("，", "+").replace(",", "+").split("+")]
        return all(part and part in normalized_text for part in parts)
    if item.item_type == "share_source":
        return msg.kind == "share_card" and pattern in apply_variants(_share_text(msg))
    if item.item_type == "contact_combo":
        return bool(extract_signals(normalized_text)) and (
            pattern == "*" or pattern in normalized_text
        )
    return False


def _message_text(msg: StandardMessage) -> str:
    return " ".join(part for part in (msg.text, _share_text(msg)) if part)


def _share_text(msg: StandardMessage) -> str:
    card = msg.share_card
    if card is None:
        return ""
    return " ".join(
        part for part in (card.source, card.title, card.prompt, card.tag, card.preview_url) if part
    )


def _category_or_other(category: str) -> Category:
    if category in ("ad", "fraud", "porn", "violence", "flood", "other"):
        return cast(Category, category)
    return "other"


def _decision(
    msg: StandardMessage,
    verdict: Verdict,
    category: Category,
    confidence: float,
    hits: list[RuleHit],
    reason: str,
) -> ModerationDecision:
    return ModerationDecision(
        message_id=msg.message_id,
        group_openid=msg.group_openid,
        sender_member_openid=msg.sender.member_openid,
        sender_role=msg.sender.role,
        verdict=verdict,
        category=category,
        confidence=round(confidence, 2),
        rule_hits=hits,
        recommended_actions=HIGH_ACTIONS if verdict == "violation_high" else [],
        reason=reason,
        is_protected_sender=msg.sender.role in ("owner", "admin"),
    )
