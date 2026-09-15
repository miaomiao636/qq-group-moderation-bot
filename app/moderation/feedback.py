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

from sqlalchemy import DateTime, Integer, String, Text, delete, func, select, update
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
CandidateStatus = Literal["PROPOSED", "COPIED_TO_DRAFT", "DISMISSED", "PURGED"]
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
    await session.flush()
    if record.label in NEGATIVE_LABELS:
        await _revoke_mapped_strikes(session, record)
    await session.commit()
    return record


async def _revoke_mapped_strikes(session: AsyncSession, feedback: FeedbackRecord) -> None:
    """Retract counting eligibility, never undo external actions or close a case."""
    shadow = await session.scalar(
        select(ShadowDecision).where(ShadowDecision.message_id == feedback.message_id)
    )
    if (
        shadow is None
        or shadow.provider not in {"onebot", "qq_official"}
        or not shadow.external_group_id
        or not shadow.external_user_id
        or not shadow.external_message_id
    ):
        return  # No verified internal-to-external mapping: do not guess a target.
    violations = (
        await session.scalars(
            select(ViolationRecord).where(
                ViolationRecord.provider == shadow.provider,
                ViolationRecord.external_group_id == shadow.external_group_id,
                ViolationRecord.external_user_id == shadow.external_user_id,
                ViolationRecord.message_id == shadow.external_message_id,
                ViolationRecord.revoked.is_(False),
            )
        )
    ).all()
    from app.models import AdminAudit

    for violation in violations:
        violation.revoked = True
        violation.revoke_reason = (
            f"反馈#{feedback.id} {feedback.label}；操作人:{feedback.operator}；"
            f"原因:{feedback.reason[:120]}"
        )[:255]
        session.add(
            AdminAudit(
                operator=feedback.operator,
                action="feedback_revoke_strike",
                target_type="violation",
                target_id=str(violation.id),
                detail_json=json.dumps(
                    {
                        "feedback_id": feedback.id,
                        "label": feedback.label,
                        "case_id": violation.case_id,
                        "external_actions_undone": False,
                    },
                    ensure_ascii=False,
                ),
            )
        )


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
    """Reconcile unhandled proposals against each message's latest human label."""
    if min_messages < 3 or min_members < 2:
        raise ValueError("自动候选至少需要3条独立消息、2个成员；明确政策请人工创建规则")
    group_providers = await _feedback_group_providers(session)
    latest = await _latest_feedback(session)
    negatives = [feedback for feedback in latest if feedback.label in NEGATIVE_LABELS]
    grouped = _group_candidate_support(latest, group_providers)
    proposed = (
        (await session.execute(select(RuleCandidate).where(RuleCandidate.status == "PROPOSED")))
        .scalars()
        .all()
    )
    existing_keys: set[tuple[str, str, str, str, str]] = set()
    mined: list[RuleCandidate] = []
    for candidate in proposed:
        provider = str(_safe_json(candidate.replay_report_json).get("provider") or "")
        key = (
            provider,
            candidate.scope_key,
            candidate.item_type,
            candidate.pattern,
            candidate.category,
        )
        existing_keys.add(key)
        if await _refresh_proposed_candidate(
            session,
            candidate,
            provider,
            grouped.get(key, []) if candidate.scope == "group" else [],
            negatives,
            min_messages=min_messages,
            min_members=min_members,
        ):
            mined.append(candidate)

    for key, examples in grouped.items():
        if key in existing_keys:
            continue
        provider, scope_key, item_type, pattern, category = key
        messages, members = _support_identity_counts(examples)
        if messages < min_messages or members < min_members:
            continue
        candidate = RuleCandidate(
            scope="group",
            scope_key=scope_key,
            item_type=item_type,
            pattern=pattern,
            category=category,
            weight=0.95,
            status="PROPOSED",
        )
        session.add(candidate)
        await session.flush()
        await _refresh_proposed_candidate(
            session,
            candidate,
            provider,
            examples,
            negatives,
            min_messages=min_messages,
            min_members=min_members,
        )
        mined.append(candidate)
    await session.commit()
    return mined


async def _latest_feedback(session: AsyncSession) -> list[FeedbackRecord]:
    # The append-only row ID orders actual writes even when timestamps tie or
    # clocks move backwards. Filter labels only AFTER selecting the last write:
    # an unknown recall explicitly retracts an earlier positive/negative truth.
    records = (await session.execute(select(FeedbackRecord).order_by(FeedbackRecord.id))).scalars()
    latest = {
        (
            record.provider,
            record.external_group_id or record.group_openid,
            record.message_id,
        ): record
        for record in records
    }
    return list(latest.values())


def _group_candidate_support(
    latest: list[FeedbackRecord], group_providers: dict[str, set[str]]
) -> dict[tuple[str, str, str, str, str], list[FeedbackRecord]]:
    grouped: dict[tuple[str, str, str, str, str], list[FeedbackRecord]] = defaultdict(list)
    for feedback in latest:
        if feedback.label not in POSITIVE_LABELS:
            continue
        scope_key = feedback.external_group_id or feedback.group_openid
        if not scope_key or len(group_providers.get(scope_key, set())) != 1:
            continue  # Missing or ambiguous legacy rule scope cannot be published safely.
        for extracted in extract_candidates_from_text(
            feedback.sample_text_masked, category=feedback.category
        ):
            grouped[
                (
                    feedback.provider,
                    scope_key,
                    extracted.item_type,
                    extracted.pattern,
                    extracted.category,
                )
            ].append(feedback)
    return dict(grouped)


def _support_identity_counts(examples: list[FeedbackRecord]) -> tuple[int, int]:
    return len({example.message_id for example in examples}), len(
        {
            example.external_user_id or example.member_openid
            for example in examples
            if example.external_user_id or example.member_openid
        }
    )


async def _refresh_proposed_candidate(
    session: AsyncSession,
    candidate: RuleCandidate,
    provider: str,
    examples: list[FeedbackRecord],
    negatives: list[FeedbackRecord],
    *,
    min_messages: int = 3,
    min_members: int = 2,
) -> bool:
    """Update only the mutable proposal/index; feedback and copied history stay intact."""
    support_count, member_count = _support_identity_counts(examples)
    conflict_count = sum(
        1
        for negative in negatives
        if negative.provider == provider
        and (negative.external_group_id or negative.group_openid) == candidate.scope_key
        and _candidate_matches_text(
            candidate.item_type, candidate.pattern, negative.sample_text_masked
        )
    )
    supported = support_count >= min_messages and member_count >= min_members
    candidate.support_count = support_count
    candidate.member_count = member_count
    candidate.conflict_count = conflict_count
    candidate.status = "PROPOSED" if supported else "DISMISSED"
    candidate.replay_report_json = json.dumps(
        {
            "provider": provider,
            "positive_coverage": support_count,
            "confirmed_negative_conflicts": conflict_count,
            "unlabeled_impact": 0,
            "support_feedback_ids": [example.id for example in examples[:20]],
            "invalidation_reason": "" if supported else "latest_feedback_support_insufficient",
        },
        ensure_ascii=False,
    )
    # This is a current support index, not the append-only feedback audit. Never
    # touch these rows for copied proposals or published rule versions.
    await session.execute(
        delete(RuleCandidateExample).where(RuleCandidateExample.candidate_id == candidate.id)
    )
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
    return supported


async def purge_dismissed_rule_candidates(
    session: AsyncSession, *, operator: str
) -> dict[str, int]:
    """Remove discarded proposal content, not IDs or published provenance.

    Keeping a minimal tombstone prevents a stale page/audit reference from
    pointing at an unrelated new candidate after SQLite reuses a deleted ID.
    Linked candidates are never cleared by this administrative list operation.
    """
    from app.models import AdminAudit

    # Acquire the SQLite writer lock before selecting candidates. The status
    # predicates and content updates then belong to one serialized transaction.
    await session.execute(
        update(RuleCandidate)
        .where(RuleCandidate.status == "DISMISSED")
        .values(status=RuleCandidate.status)
        .execution_options(synchronize_session=False)
    )
    candidates = (
        await session.scalars(
            select(RuleCandidate)
            .where(RuleCandidate.status == "DISMISSED")
            .execution_options(populate_existing=True)
        )
    ).all()
    purged_ids: list[int] = []
    kept_linked = 0
    for candidate in candidates:
        if candidate.copied_version_id is not None:
            kept_linked += 1
            continue
        candidate.status = "PURGED"
        candidate.pattern = ""
        candidate.replay_report_json = "{}"
        purged_ids.append(candidate.id)
    if purged_ids:
        await session.execute(
            delete(RuleCandidateExample).where(RuleCandidateExample.candidate_id.in_(purged_ids))
        )
    result = {"purged": len(purged_ids), "kept_linked": kept_linked}
    session.add(
        AdminAudit(
            operator=operator[:64],
            action="feedback_purge_dismissed",
            target_type="rule_candidate",
            target_id="batch",
            detail_json=json.dumps(result),
        )
    )
    await session.commit()
    return result


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
    if candidate.status not in {"PROPOSED", "COPIED_TO_DRAFT"}:
        raise ValueError("已忽略或已清理的候选规则不能复制")
    providers = (await _feedback_group_providers(session)).get(candidate.scope_key, set())
    candidate_provider = _safe_json(candidate.replay_report_json).get("provider")
    if candidate.scope != "group" or not candidate_provider or providers != {candidate_provider}:
        raise ValueError("候选规则来源provider缺失或群来源有歧义，请重新挖掘并确认来源")
    latest = await _latest_feedback(session)
    grouped = _group_candidate_support(latest, {candidate.scope_key: providers})
    examples = grouped.get(
        (
            str(candidate_provider),
            candidate.scope_key,
            candidate.item_type,
            candidate.pattern,
            candidate.category,
        ),
        [],
    )
    messages, members = _support_identity_counts(examples)
    if candidate.status == "PROPOSED":
        await _refresh_proposed_candidate(
            session,
            candidate,
            str(candidate_provider),
            examples,
            [feedback for feedback in latest if feedback.label in NEGATIVE_LABELS],
        )
        await session.commit()
    if messages < 3 or members < 2:
        raise ValueError("最新人工标签的候选支持不足，请重新挖掘后审核")

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


async def _feedback_group_providers(session: AsyncSession) -> dict[str, set[str]]:
    """Legacy dynamic-rule scopes have no provider column: reject collisions."""
    providers: dict[str, set[str]] = defaultdict(set)
    for model in (FeedbackRecord, ShadowDecision, ViolationRecord):
        group_id = func.coalesce(func.nullif(model.external_group_id, ""), model.group_openid)
        rows = (await session.execute(select(group_id, model.provider).distinct())).all()
        for group, provider in rows:
            if group and provider:
                providers[str(group)].add(str(provider))
    return dict(providers)


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
    """Compatibility no-op: unreviewed sample labels never become cloud prompts.

    Retaining this entry point keeps older callers safe. Structured feedback is
    still stored and mined into candidates; general rules require explicit
    replay, draft publication and versioning. Neither labels nor free-text
    reasons are uploaded by this former immediate-learning path.
    """
    return ""
