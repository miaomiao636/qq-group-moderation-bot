"""Administrator feedback and candidate-rule mining.

T-205: Human feedback is the only truth source. Unknown recalls stay unlabeled;
candidate rules are proposals only and can only be copied to dynamic-rule drafts.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.cases.models import ViolationRecord
from app.db import Base
from app.moderation.ai import sanitize_text
from app.runtime.models import ShadowDecision

FeedbackLabel = Literal[
    "confirmed_violation",
    "confirmed_normal",
    "false_positive",
    "unknown_recall",
    "other_recall",
]
CandidateStatus = Literal["PROPOSED", "COPIED_TO_DRAFT", "DISMISSED"]
CandidateItemType = Literal["keyword", "phrase_combo", "domain", "share_source", "contact_combo"]

POSITIVE_LABELS: set[str] = {"confirmed_violation"}
NEGATIVE_LABELS: set[str] = {"confirmed_normal", "false_positive"}
UNKNOWN_LABELS: set[str] = {"unknown_recall", "other_recall"}
_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)
_SPLIT_RE = re.compile(r"[\s,，。.!！?？;；:：、/\\|]+")
_CONTACT_MARKER = "[CONTACT_MASKED]"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FeedbackRecord(Base):
    """Human feedback for one stored message or case evidence item."""

    __tablename__ = "feedback_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(128), index=True)
    group_openid: Mapped[str] = mapped_column(String(128), index=True, default="")  # 旧镜像
    member_openid: Mapped[str] = mapped_column(String(128), index=True, default="")  # 旧镜像
    # T-305 传输中立身份（与镜像字段双写，权威读取口径）
    provider: Mapped[str] = mapped_column(
        String(16), default="qq_official", server_default="qq_official"
    )
    external_group_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    external_user_id: Mapped[str] = mapped_column(String(128), default="", server_default="")
    label: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32), default="other")
    operator: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64), default="web")
    reason: Mapped[str] = mapped_column(String(255), default="")
    sample_text_masked: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RuleCandidate(Base):
    """A mined rule proposal that is not active until copied and published."""

    __tablename__ = "rule_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), default="group")
    scope_key: Mapped[str] = mapped_column(String(128), index=True)
    item_type: Mapped[str] = mapped_column(String(32))
    pattern: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(32), default="ad")
    weight: Mapped[float] = mapped_column(default=0.95)
    support_count: Mapped[int] = mapped_column(Integer, default=0)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="PROPOSED", index=True)
    replay_report_json: Mapped[str] = mapped_column(Text, default="{}")
    generated_by: Mapped[str] = mapped_column(String(64), default="local-miner")
    copied_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RuleCandidateExample(Base):
    """Trace candidate support back to feedback records without storing secrets."""

    __tablename__ = "rule_candidate_examples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(Integer, index=True)
    feedback_id: Mapped[int] = mapped_column(Integer, index=True)
    message_id: Mapped[str] = mapped_column(String(128), index=True)
    member_openid: Mapped[str] = mapped_column(String(128), default="")
    label: Mapped[str] = mapped_column(String(32), default="")


@dataclass(frozen=True)
class ExtractedCandidate:
    item_type: CandidateItemType
    pattern: str
    category: str


async def record_feedback(
    session: AsyncSession,
    message_id: str,
    label: str,
    category: str,
    operator: str,
    reason: str = "",
    *,
    source: str = "web",
    group_openid: str = "",
    member_openid: str = "",
    sample_text: str = "",
) -> FeedbackRecord:
    """Record human feedback. Unknown recalls are stored but not used as truth."""
    label_clean = _validate_label(label)
    category_clean = category.strip().lower() or "other"
    metadata = await _resolve_message_metadata(session, message_id)
    text = sample_text or metadata.get("text", "")
    record = FeedbackRecord(
        message_id=message_id,
        group_openid=(group_openid or metadata.get("group_openid", ""))[:128],
        member_openid=(member_openid or metadata.get("member_openid", ""))[:128],
        provider=str(metadata.get("provider") or "qq_official")[:16],
        external_group_id=str(metadata.get("external_group_id") or group_openid or "")[:128],
        external_user_id=str(metadata.get("external_user_id") or member_openid or "")[:128],
        label=label_clean,
        category=category_clean[:32],
        operator=operator[:64],
        source=source[:64],
        reason=reason[:255],
        sample_text_masked=sanitize_text(text)[:1_000],
    )
    session.add(record)
    await session.commit()
    return record


async def record_recall_notice(
    session: AsyncSession,
    *,
    message_id: str,
    group_openid: str = "",
    member_openid: str = "",
    operator: str = "onebot_group_recall",
) -> str:
    """管理员撤回通知 → 自动记录「未知原因撤回」反馈（T-306增强）。

    撤回原因未知：不当真值、不进入违规列表、不产生任何动作，
    仅作为待人工确认线索；同一消息只记录一次（幂等，重复通知/
    断线重连/重启均不重复记录）。

    返回："recorded"（已记录）/ "duplicate"（该消息已有反馈，跳过）/
    "ignored"（message_id为空）。
    """
    mid = message_id.strip()
    if not mid:
        return "ignored"
    exists = (
        (await session.execute(select(FeedbackRecord).where(FeedbackRecord.message_id == mid)))
        .scalars()
        .first()
    )
    if exists is not None:
        return "duplicate"
    await record_feedback(
        session,
        mid,
        "unknown_recall",
        "other",
        operator,
        reason="管理员撤回该消息，原因未知（NapCat撤回通知自动记录，待人工确认）",
        source="onebot_recall",
        group_openid=group_openid,
        member_openid=member_openid,
    )
    return "recorded"


async def mine_rule_candidates(
    session: AsyncSession,
    *,
    min_messages: int = 3,
    min_members: int = 2,
) -> list[RuleCandidate]:
    """Mine structured rule candidates from confirmed positive feedback only."""
    positives = (
        (
            await session.execute(
                select(FeedbackRecord)
                .where(FeedbackRecord.label.in_(POSITIVE_LABELS))
                .order_by(FeedbackRecord.created_at.asc(), FeedbackRecord.id.asc())
            )
        )
        .scalars()
        .all()
    )
    negatives = (
        (
            await session.execute(
                select(FeedbackRecord).where(FeedbackRecord.label.in_(NEGATIVE_LABELS))
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[tuple[str, str, str, str, str], list[FeedbackRecord]] = defaultdict(list)
    for feedback in positives:
        for extracted in extract_candidates_from_text(
            feedback.sample_text_masked, category=feedback.category
        ):
            scope_key = feedback.group_openid or "*"
            grouped[
                (
                    "group" if feedback.group_openid else "global",
                    scope_key,
                    extracted.item_type,
                    extracted.pattern,
                    extracted.category,
                )
            ].append(feedback)

    created: list[RuleCandidate] = []
    for (scope, scope_key, item_type, pattern, category), examples in grouped.items():
        message_ids = {example.message_id for example in examples}
        member_ids = {example.member_openid for example in examples if example.member_openid}
        if len(message_ids) < min_messages or len(member_ids) < min_members:
            continue
        conflict_count = sum(
            1
            for negative in negatives
            if negative.group_openid in (scope_key, "")
            and _candidate_matches_text(item_type, pattern, negative.sample_text_masked)
        )
        report = {
            "positive_coverage": len(message_ids),
            "confirmed_negative_conflicts": conflict_count,
            "unlabeled_impact": 0,
            "support_feedback_ids": [example.id for example in examples[:20]],
        }
        candidate = RuleCandidate(
            scope=scope,
            scope_key=scope_key,
            item_type=item_type,
            pattern=pattern,
            category=category,
            weight=0.95,
            support_count=len(message_ids),
            member_count=len(member_ids),
            conflict_count=conflict_count,
            replay_report_json=json.dumps(report, ensure_ascii=False),
        )
        session.add(candidate)
        await session.flush()
        for example in examples[:20]:
            session.add(
                RuleCandidateExample(
                    candidate_id=candidate.id,
                    feedback_id=example.id,
                    message_id=example.message_id,
                    member_openid=example.member_openid,
                    label=example.label,
                )
            )
        created.append(candidate)
    await session.commit()
    return created


async def copy_candidate_to_draft(
    session: AsyncSession,
    candidate_id: int,
    *,
    operator: str,
    draft_name: str = "",
) -> int:
    """Copy a candidate into a dynamic-rule draft. It remains inactive."""
    candidate = await session.get(RuleCandidate, candidate_id)
    if candidate is None:
        raise ValueError("候选规则不存在")
    if candidate.status == "DISMISSED":
        raise ValueError("已忽略的候选规则不能复制")

    from app.moderation.dynamic_rules import add_rule_item, create_rule_draft

    draft = await create_rule_draft(
        session,
        scope="group" if candidate.scope == "group" else "global",
        scope_key=candidate.scope_key,
        name=draft_name or f"候选规则#{candidate.id}",
        operator=operator,
    )
    await add_rule_item(
        session,
        draft.id,
        item_type=candidate.item_type,
        pattern=candidate.pattern,
        category=candidate.category,
        weight=candidate.weight,
        description=f"来自反馈候选#{candidate.id}",
        operator=operator,
    )
    candidate.status = "COPIED_TO_DRAFT"
    candidate.copied_version_id = draft.id
    await session.commit()
    return draft.id


def extract_candidates_from_text(text: str, *, category: str) -> list[ExtractedCandidate]:
    """Extract safe structured candidates from masked text."""
    safe_text = sanitize_text(text)
    results: dict[tuple[str, str], ExtractedCandidate] = {}
    for domain in _DOMAIN_RE.findall(safe_text):
        _put_candidate(results, "domain", domain.lower(), category)
    if _CONTACT_MARKER in safe_text:
        _put_candidate(results, "contact_combo", "*", category)
    for part in _SPLIT_RE.split(safe_text):
        part = part.strip()
        if not _usable_phrase(part):
            continue
        if len(part) <= 12:
            _put_candidate(results, "keyword", part, category)
        else:
            for phrase in _phrase_windows(part):
                _put_candidate(results, "keyword", phrase, category)
    return list(results.values())


def _put_candidate(
    target: dict[tuple[str, str], ExtractedCandidate],
    item_type: CandidateItemType,
    pattern: str,
    category: str,
) -> None:
    clean = pattern.strip()
    if not clean or "[" in clean or "]" in clean:
        return
    target[(item_type, clean)] = ExtractedCandidate(item_type, clean, category or "other")


def _phrase_windows(text: str) -> set[str]:
    """整段作为一个关键词（不再滑窗碎片），截断到30字防过长。"""
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text)
    compact = compact[:30]
    if len(compact) < 4 or not _usable_phrase(compact):
        return set()
    return {compact}


def _usable_phrase(text: str) -> bool:
    if len(text) < 2 or len(text) > 30:
        return False
    if text == _CONTACT_MARKER:
        return False
    return not text.isdigit()


def _candidate_matches_text(item_type: str, pattern: str, text: str) -> bool:
    safe_text = sanitize_text(text)
    if item_type == "contact_combo":
        return _CONTACT_MARKER in safe_text
    return pattern in safe_text


def _validate_label(label: str) -> FeedbackLabel:
    label_clean = label.strip().lower()
    if label_clean not in POSITIVE_LABELS | NEGATIVE_LABELS | UNKNOWN_LABELS:
        raise ValueError("不支持的反馈标签")
    return cast(FeedbackLabel, label_clean)


async def _resolve_message_metadata(session: AsyncSession, message_id: str) -> dict[str, str]:
    shadow = await session.scalar(
        select(ShadowDecision).where(ShadowDecision.message_id == message_id)
    )
    if shadow is not None:
        detail = _safe_json(shadow.detail_json)
        return {
            "group_openid": shadow.group_openid,
            "member_openid": shadow.member_openid,
            "provider": shadow.provider or "qq_official",
            "external_group_id": shadow.external_group_id or shadow.group_openid,
            "external_user_id": shadow.external_user_id or shadow.member_openid,
            "text": str(detail.get("text_preview") or ""),
        }
    violation = await session.scalar(
        select(ViolationRecord).where(ViolationRecord.message_id == message_id)
    )
    if violation is not None:
        snapshot = _safe_json(violation.message_snapshot_json)
        sender_raw = snapshot.get("sender")
        sender = sender_raw if isinstance(sender_raw, dict) else {}
        return {
            "group_openid": violation.group_openid,
            "member_openid": violation.member_openid or str(sender.get("member_openid") or ""),
            "provider": violation.provider or "qq_official",
            "external_group_id": violation.external_group_id or violation.group_openid,
            "external_user_id": violation.external_user_id or violation.member_openid,
            "text": str(snapshot.get("text") or ""),
        }
    return {
        "group_openid": "",
        "member_openid": "",
        "provider": "qq_official",
        "external_group_id": "",
        "external_user_id": "",
        "text": "",
    }


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def load_vision_feedback_context(
    session: AsyncSession,
    *,
    provider: str = "",
    external_group_id: str = "",
) -> str:
    """加载人工确认的图片反馈，蒸馏成纠正上下文喂给视觉AI。

    P0-4修复：
    - 按 provider + external_group_id 隔离（不跨群跨通道汇总）；
    - 使用传输中立复合身份 join（provider+external_group_id+message_id）；
    - 不把管理员自由文本 reason 发给云端，只用结构化 label 映射。
    """
    from sqlalchemy import and_

    join_cond = and_(
        FeedbackRecord.message_id == ShadowDecision.message_id,
        FeedbackRecord.provider == ShadowDecision.provider,
    )
    where_conds = [
        ShadowDecision.kind == "image",
        FeedbackRecord.label.in_(tuple(POSITIVE_LABELS | NEGATIVE_LABELS)),
    ]
    if provider:
        where_conds.append(FeedbackRecord.provider == provider)
    if external_group_id:
        where_conds.append(FeedbackRecord.external_group_id == external_group_id)

    rows = (
        await session.execute(
            select(FeedbackRecord, ShadowDecision)
            .join(ShadowDecision, join_cond)
            .where(*where_conds)
            .order_by(FeedbackRecord.id.desc())
            .limit(20)
        )
    ).all()

    # P0-4: 结构化映射——不发送管理员自由文本 reason
    _LABEL_MAP = {
        "confirmed_violation": "violation",
        "confirmed_normal": "normal",
        "false_positive": "normal",
    }
    corrections: list[str] = []
    reinforcements: list[str] = []
    for fb, shadow in rows:
        detail = _safe_json(shadow.detail_json)
        ai_results = detail.get("ai_results") or []
        ai_cat = None
        for r in ai_results:
            if isinstance(r, dict) and r.get("category"):
                ai_cat = r.get("category")
                break
        human_label = _LABEL_MAP.get(fb.label, "unknown")
        if human_label == "normal" and ai_cat in ("ad", "fraud"):
            corrections.append("prev_ad_but_human_normal->null")
        elif human_label == "violation" and ai_cat in (None, "other", "normal"):
            corrections.append(f"prev_{ai_cat or 'null'}_but_human_violation->ad")
        elif human_label == "violation" and ai_cat in ("ad", "fraud"):
            reinforcements.append(f"human_confirmed_{ai_cat}")

    parts: list[str] = []
    if corrections:
        seen = set()
        unique_corr = []
        for c in corrections:
            if c not in seen:
                seen.add(c)
                unique_corr.append(c)
        parts.append("corrections:" + ",".join(unique_corr[:5]))
    if reinforcements:
        parts.append("reinforcements:" + ",".join(reinforcements[:3]))
    return " | ".join(parts) if parts else ""
