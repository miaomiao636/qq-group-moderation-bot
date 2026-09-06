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
    group_openid: Mapped[str] = mapped_column(String(128), index=True, default="")
    member_openid: Mapped[str] = mapped_column(String(128), index=True, default="")
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
    phrases: set[str] = set()
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", text)
    if len(compact) < 4:
        return phrases
    for size in range(4, min(8, len(compact)) + 1):
        for idx in range(0, len(compact) - size + 1):
            phrase = compact[idx : idx + size]
            if _usable_phrase(phrase):
                phrases.add(phrase)
    return phrases


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
            "text": str(snapshot.get("text") or ""),
        }
    return {"group_openid": "", "member_openid": "", "text": ""}


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
