"""数据保留清理（T-402）。

保留期阈值来自 settings，不在清理器内改变部署政策：
- RAW_RETENTION_DAYS 控制媒体、消息快照及影子判定/反馈中的原文副本；
- DECISION_RETENTION_DAYS 控制动作日志、已结束 inbox 与通知记录；
- AI 缓存超过自身 TTL 或原始期即删除。
违规计数、精确身份映射、人工标签、规则版本和必要审计元数据保留。
原文过期后只能追溯记录关联，不能承诺仍有完整的原始内容供人工复核。
"""

from __future__ import annotations

import json
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import Case, ViolationRecord
from app.config import get_settings
from app.models import ActionLog, ProcessedEvent

_PURGED_SNAPSHOT = '{"purged": true, "reason": "raw_retention_expired"}'
_PURGED_REASON = "原始内容已按保留期清理"
_RULE_METADATA = frozenset({"rule_id", "rule_name", "category", "confidence_delta"})
_AI_METADATA = frozenset(
    {
        "category",
        "confidence",
        "model_id",
        "prompt_version",
        "provider",
        "source",
        "needs_review",
        "latency_ms",
        "cost_cents",
        "raw_response_sha256",
        "review_role",
        "review_group",
        "policy_version",
        "cache_hit",
        "input_tokens",
        "output_tokens",
        "cost_known",
    }
)
_ACTION_METADATA = frozenset(
    {
        "id",
        "action",
        "status",
        "provider",
        "message_id",
        "external_message_id",
        "external_group_id",
        "external_user_id",
        "target_member_openid",
    }
)


def _parse_json(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _metadata_items(value: Any, keys: frozenset[str]) -> list[dict[str, Any]]:
    """Retain only declared scalar metadata, never unknown/nested original payloads."""
    if not isinstance(value, list):
        return []
    return [
        {key: item[key] for key in keys if key in item and not isinstance(item[key], (dict, list))}
        for item in value
        if isinstance(item, dict)
    ]


def _shadow_metadata(raw: str) -> str:
    detail = _parse_json(raw, {})
    if not isinstance(detail, dict):
        detail = {}
    ai_rows = detail.get("ai_results")
    ai_rows = [row for row in ai_rows if isinstance(row, dict)] if isinstance(ai_rows, list) else []
    ai_metadata = _metadata_items(ai_rows, _AI_METADATA)
    for original, kept in zip(ai_rows, ai_metadata, strict=True):
        # Preserve failure semantics for historical reports without preserving
        # a provider's potentially content-bearing error/reason string.
        kept["degraded_reason"] = (
            "raw_retention_expired_degraded" if original.get("degraded_reason") else ""
        )
    cleaned: dict[str, Any] = {
        "purged": True,
        "reason": "raw_retention_expired",
        "text_preview": "",
        "rule_hits": _metadata_items(detail.get("rule_hits"), _RULE_METADATA),
        "ai_results": ai_metadata,
        "action_intents": _metadata_items(detail.get("action_intents"), _ACTION_METADATA),
    }
    # Message chronology is non-content metadata. Keep only a parsed, canonical
    # timestamp; never preserve an arbitrary original string under a time key.
    raw_sent = detail.get("sent_at")
    if isinstance(raw_sent, str) and len(raw_sent) <= 64:
        try:
            sent = datetime.fromisoformat(raw_sent)
            sent = sent.replace(tzinfo=UTC) if sent.tzinfo is None else sent.astimezone(UTC)
            cleaned["sent_at"] = sent.isoformat()
        except (ValueError, OverflowError):
            pass
    versions = detail.get("rule_version_ids")
    if isinstance(versions, list):
        cleaned["rule_version_ids"] = [item for item in versions if type(item) is int]
    actions = detail.get("recommended_actions")
    if isinstance(actions, list):
        cleaned["recommended_actions"] = [
            item for item in actions if item in ("recall", "mute", "warn")
        ]
    if type(detail.get("is_protected_sender")) is bool:
        cleaned["is_protected_sender"] = detail["is_protected_sender"]
    # No media filenames/URLs or segment text survive. Message identity remains
    # in columns, so feedback correction does not depend on a raw-content blob.
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True)


def _case_reference_ids(raw: str) -> set[int] | None:
    """A malformed reference is not an empty reference or authority to erase."""
    value = _parse_json(raw, None)
    if not isinstance(value, list) or any(type(item) is not int or item < 1 for item in value):
        return None
    return set(value)


def _case_audit_metadata(audit: dict[str, Any], now: datetime) -> str:
    """Keep accountability, not free-form originals/confirmation codes in extras."""
    cleaned: dict[str, Any] = {"lifecycle_purged_at": now.replace(tzinfo=UTC).isoformat()}
    if type(audit.get("evidence_count")) is int:
        cleaned["evidence_count"] = audit["evidence_count"]
    if isinstance(audit.get("revoked_by"), str):
        cleaned["revoked_by"] = audit["revoked_by"][:64]
    if audit.get("revoke_reason"):
        cleaned["revoke_reason"] = _PURGED_REASON
    cleaned["transitions"] = _metadata_items(
        audit.get("transitions"), frozenset({"from", "to", "operator", "at"})
    )
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True)


async def _purge_case_lifecycle(
    session: AsyncSession, *, now: datetime, raw_cutoff: datetime
) -> dict[str, int]:
    """Hide completed cases after 15d; clear eligible content after 90d archived.

    Case and violation IDs remain durable: both are referenced by audit/feedback,
    and SQLite may reuse a physically deleted maximum ID. Neither archive age
    nor a case's JSON list proves that a violation has no other live consumers.
    Raw-source redaction has its own TTL above and is never deferred here.
    """
    archive_cutoff = now - timedelta(days=15)
    result = await session.execute(
        update(Case)
        .where(
            Case.archived.is_(False),
            Case.status == "CLOSED",
            Case.closed_at.is_not(None),
            Case.closed_at < archive_cutoff,
        )
        .values(archived=True, archived_at=now)
        .execution_options(synchronize_session=False)
    )
    counts = {
        "cases_archived": int(getattr(result, "rowcount", 0) or 0),
        "cases_purged": 0,
        "cases_cleanup_deferred": 0,
        "violation_records_purged": 0,
    }
    old_cases = (
        await session.scalars(
            select(Case).where(
                Case.archived.is_(True),
                Case.archived_at.is_not(None),
                Case.archived_at < now - timedelta(days=90),
            )
        )
    ).all()
    if not old_cases:
        return counts
    reference_owners: dict[int, set[int]] = {}
    malformed_cases: set[int] = set()
    for case_id, raw_ids in await session.execute(select(Case.id, Case.violation_ids_json)):
        ids = _case_reference_ids(raw_ids)
        if ids is None:
            malformed_cases.add(case_id)
            continue
        for violation_id in ids:
            reference_owners.setdefault(violation_id, set()).add(case_id)
    for case in old_cases:
        audit = _parse_json(case.audit_json, None)
        if isinstance(audit, dict) and audit.get("lifecycle_purged_at"):
            continue  # Count only an actual first transition, not each cron visit.
        ids = _case_reference_ids(case.violation_ids_json)
        if (
            case.status != "CLOSED"
            or case.closed_at is None
            or case.archived_at is None
            or case.closed_at.replace(tzinfo=None) >= archive_cutoff
            or case.archived_at.replace(tzinfo=None) < case.closed_at.replace(tzinfo=None)
            or ids is None
            or not isinstance(audit, dict)
            or malformed_cases
        ):
            counts["cases_cleanup_deferred"] += 1
            continue
        records = (
            await session.scalars(
                select(ViolationRecord).where(
                    or_(ViolationRecord.id.in_(ids), ViolationRecord.case_id == case.id)
                )
            )
        ).all()
        # Include reverse links: later violations may use an existing case while
        # its original JSON evidence list remains unchanged.
        unsafe = ids - {record.id for record in records}
        for record in records:
            if (
                record.created_at.replace(tzinfo=None) >= min(raw_cutoff, now - timedelta(days=30))
                or record.case_id not in (None, case.id)
                or reference_owners.get(record.id, set()) - {case.id}
                or record.provider != case.provider
                or (record.external_group_id or record.group_openid)
                != (case.external_group_id or case.group_openid)
                or (record.external_user_id or record.member_openid)
                != (case.external_user_id or case.member_openid)
            ):
                unsafe.add(record.id)
        if unsafe:
            counts["cases_cleanup_deferred"] += 1
            continue
        case.violation_ids_json = "[]"
        case.audit_json = _case_audit_metadata(audit, now)
        counts["cases_purged"] += 1
    return counts


async def _purge_content_copies(
    session: AsyncSession, raw_cutoff: datetime, now: datetime
) -> dict[str, int]:
    from app.moderation.ai import AICacheEntry
    from app.moderation.feedback import (
        POSITIVE_LABELS,
        FeedbackRecord,
        RuleCandidate,
        RuleCandidateExample,
        _latest_feedback,
        extract_candidates_from_text,
    )
    from app.runtime.models import ShadowDecision

    shadow_count = 0
    for shadow in await session.scalars(
        select(ShadowDecision).where(ShadowDecision.created_at < raw_cutoff)
    ):
        redacted = _shadow_metadata(shadow.detail_json)
        if (shadow.detail_json, shadow.reason, shadow.sender_name) != (
            redacted,
            _PURGED_REASON,
            "",
        ):
            shadow.detail_json = redacted
            shadow.reason = _PURGED_REASON
            shadow.sender_name = ""
            shadow_count += 1

    # A late feedback copy cannot restart an old source message's retention.
    # Match exact message + provider + group + member, with legacy mirror fallback.
    def identity(column: Any, mirror: Any) -> Any:
        return func.coalesce(func.nullif(column, ""), mirror)

    feedback_group = identity(FeedbackRecord.external_group_id, FeedbackRecord.group_openid)
    feedback_member = identity(FeedbackRecord.external_user_id, FeedbackRecord.member_openid)
    expired_sources = []
    for model in (ShadowDecision, ViolationRecord):
        expired_sources.append(
            select(model.message_id)
            .where(
                model.message_id == FeedbackRecord.message_id,
                model.provider == FeedbackRecord.provider,
                identity(model.external_group_id, model.group_openid) == feedback_group,
                identity(model.external_user_id, model.member_openid) == feedback_member,
                model.created_at < raw_cutoff,
            )
            .exists()
        )
    expired_feedback = or_(FeedbackRecord.created_at < raw_cutoff, *expired_sources)
    feedback_count = 0
    expired_support: set[tuple[str, str, str, str, str]] = set()
    for feedback in await session.scalars(select(FeedbackRecord).where(expired_feedback)):
        # A recent proposal may already have lost its example index after a
        # negative relabel. Capture its source key before erasing the old text,
        # regardless of that source's current label.
        for extracted in extract_candidates_from_text(
            feedback.sample_text_masked, category=feedback.category
        ):
            expired_support.add(
                (
                    feedback.provider,
                    feedback.external_group_id or feedback.group_openid,
                    extracted.item_type,
                    extracted.pattern,
                    extracted.category,
                )
            )
        if feedback.sample_text_masked or (feedback.reason and feedback.reason != _PURGED_REASON):
            feedback.sample_text_masked = ""
            feedback.reason = _PURGED_REASON
            feedback_count += 1

    # Auto-mined patterns can be entire original sentences. Redact only unhandled
    # automatic proposals with expired sources and no surviving latest support.
    # A dismissed candidate may have lost its current support index; its own age
    # still bounds the source messages from which local-miner originally made it.
    expired_candidate_ids = set(
        await session.scalars(
            select(RuleCandidateExample.candidate_id)
            .join(FeedbackRecord, FeedbackRecord.id == RuleCandidateExample.feedback_id)
            .where(expired_feedback)
        )
    )
    candidates = (
        await session.scalars(
            select(RuleCandidate).where(
                RuleCandidate.generated_by == "local-miner",
                RuleCandidate.status.in_(("PROPOSED", "DISMISSED")),
                RuleCandidate.copied_version_id.is_(None),
            )
        )
    ).all()
    live_support: set[tuple[str, str, str, str, str]] = set()
    if candidates:
        # Expired-source copies were cleared above, including late feedback;
        # neither a new label timestamp nor a superseded positive renews support.
        for feedback in await _latest_feedback(session):
            if feedback.label not in POSITIVE_LABELS:
                continue
            for extracted in extract_candidates_from_text(
                feedback.sample_text_masked, category=feedback.category
            ):
                live_support.add(
                    (
                        feedback.provider,
                        feedback.external_group_id or feedback.group_openid,
                        extracted.item_type,
                        extracted.pattern,
                        extracted.category,
                    )
                )
    candidate_count = 0
    for candidate in candidates:
        marker = f"raw_retention_purged_candidate_{candidate.id}"
        report = _parse_json(candidate.replay_report_json, {})
        report = report if isinstance(report, dict) else {}
        key = (
            str(report.get("provider") or ""),
            candidate.scope_key,
            candidate.item_type,
            candidate.pattern,
            candidate.category,
        )
        if candidate.pattern == marker or key in live_support:
            continue
        if not (
            candidate.created_at.replace(tzinfo=None) < raw_cutoff
            or candidate.id in expired_candidate_ids
            or key in expired_support
        ):
            continue
        candidate.pattern = marker
        candidate.status = "DISMISSED"
        report["invalidation_reason"] = "raw_retention_expired"
        candidate.replay_report_json = json.dumps(report, ensure_ascii=False)
        candidate_count += 1

    cache_result = await session.execute(
        delete(AICacheEntry).where(
            or_(AICacheEntry.created_at < raw_cutoff, AICacheEntry.expires_at <= now)
        )
    )
    return {
        "shadow_content_purged": shadow_count,
        "feedback_content_purged": feedback_count,
        "candidate_patterns_purged": candidate_count,
        "ai_cache_deleted": int(getattr(cache_result, "rowcount", 0) or 0),
    }


@dataclass(frozen=True)
class _ManagedCopy:
    """登记在册的副本路径（相对 ``data/``；末层可用 ``*`` 匹配）。

    只清理登记项命中的路径——**不扫未知目录**（D-026：不得按未知目录批量删除）。
    ``keep_min_entries`` 按"顶层条目"（文件或子目录）**保底保留最新的 N 个**，
    用于备份类副本的恢复生命线；其余条目内文件按 mtime 超期删除。
    """

    pattern: str
    retention_days: int
    keep_min_entries: int = 0
    note: str = ""


# 副本登记表（"15 天全副本工程"）：所有原内容副本按原消息时间最多 15 天
# （D-024/D-026）。新增副本目录必须在此登记（走代码审查）。工作目录
# （如待标注的 sample_pool）不登记即不触碰。
_MANAGED_COPIES: tuple[_ManagedCopy, ...] = (
    _ManagedCopy("media_snapshot_*", 15, note="手工媒体快照（含 _frames）"),
    _ManagedCopy("t002_media", 15, note="T-002 时代归档"),
    _ManagedCopy("backup_shadow_*.json", 15, note="影子数据手工导出"),
    _ManagedCopy("_dbg_tmp", 3, note="调试残留（短期限）"),
    _ManagedCopy("media/_frames", 15, note="视频帧残留（正常流程应即时清理）"),
    _ManagedCopy("backups", 15, keep_min_entries=1, note="备份集（保底最新 1 份）"),
    _ManagedCopy("purge_drill", 7, note="清理演练目录（可复现）"),
    _ManagedCopy("loadtest", 7, note="容量压测隔离库（可复现）"),
)


def _managed_copy_root() -> Path:
    """登记根 = 媒体目录所在的数据根。

    跟随 `pipeline.MEDIA_DIR`（含测试/演练对它的 monkeypatch）——清理只作用
    于与当前运行环境一致的 data 根；测试环境因此天然隔离，不会触碰真实 data/。
    """
    from app.runtime.pipeline import MEDIA_DIR

    return MEDIA_DIR.parent


_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link_like(p: Path) -> bool:
    """符号链接 / Windows junction / 其他重解析点——一律拒绝（R-112 N01）。

    登记名若被链接到根内未登记目录，旧实现仅校验 resolve 后仍在 data 根内，
    会跟随链接删除未登记目录的原文件，违反 D-026"不得按未知目录批量删除"。
    遍历与删除前均须先过此关；无法确认属性时按链接处理（fail-closed）。
    """
    try:
        if p.is_symlink():
            return True
        st = p.lstat()
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _resolve_managed_targets(root: Path, pattern: str) -> list[Path]:
    """解析登记 pattern：仅 data 根内、单层 glob；链接与越界结果直接丢弃。"""
    if ".." in Path(pattern).parts:
        return []
    try:
        root_resolved = root.resolve()
    except OSError:
        return []
    matches = sorted(root.glob(pattern)) if "*" in pattern else [root / pattern]
    out: list[Path] = []
    for m in matches:
        if not m.exists():
            continue
        if _is_link_like(m):
            continue  # R-112 N01：登记名不得是链接/重解析点（根内别名绕行）
        try:
            rp = m.resolve()
        except OSError:
            continue
        if rp.parent == root_resolved or root_resolved in rp.parents:
            out.append(m)
    return out


def _managed_entry_files(target: Path) -> list[Path]:
    """登记目录内的全部普通文件；**不跟随任何链接/重解析点**（R-112 N01）。

    旧实现用 rglob，在部分 Python 版本会跟随目录链接进入未登记目录。
    改为显式栈式遍历：任何链接（文件或目录）一律跳过；返回顺序确定。
    """
    if _is_link_like(target):
        return []
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []
    out: list[Path] = []
    stack: list[Path] = [target]
    while stack:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if _is_link_like(entry):
                continue
            try:
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    out.append(entry)
            except OSError:
                continue
    return sorted(out)


def _managed_keep_set(target: Path, entry: _ManagedCopy) -> set[Path]:
    """保底条目（最新 N 个顶层条目）内的全部文件。

    条目新鲜度按**条目内最新文件的 mtime** 排序——目录自身的 mtime 只反映
    创建/改名时间，不能代表备份内容时间。
    """
    if entry.keep_min_entries <= 0 or not target.is_dir():
        return set()
    try:
        pairs = [(item, _managed_entry_files(item)) for item in target.iterdir()]
    except OSError:
        return set()

    def _freshness(pair: tuple[Path, list[Path]]) -> float:
        item, files = pair
        try:
            return max((f.stat().st_mtime for f in files), default=item.stat().st_mtime)
        except OSError:
            return 0.0

    kept: set[Path] = set()
    for _item, files in sorted(pairs, key=_freshness, reverse=True)[: entry.keep_min_entries]:
        kept.update(files)
    return kept


def _scan_managed_copies(ts: float, root: Path) -> list[tuple[_ManagedCopy, Path]]:
    """扫描登记副本，返回（登记项, 到期文件）列表；不修改任何文件。"""
    expired: list[tuple[_ManagedCopy, Path]] = []
    for entry in _MANAGED_COPIES:
        cutoff = ts - entry.retention_days * 86400
        for target in _resolve_managed_targets(root, entry.pattern):
            keep = _managed_keep_set(target, entry)
            for f in _managed_entry_files(target):
                if f in keep:
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    expired.append((entry, f))
    return expired


def plan_managed_copies(now: float | None = None, *, root: Path | None = None) -> dict[str, Any]:
    """dry-run：只读报告登记副本中"将到期删除"的文件清单。"""
    ts = time.time() if now is None else now
    root = root or _managed_copy_root()
    items: list[dict[str, Any]] = []
    total = 0
    for entry, f in _scan_managed_copies(ts, root):
        try:
            st = f.stat()
        except OSError:
            continue
        total += st.st_size
        items.append(
            {
                "path": str(f),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, UTC).strftime("%m-%d %H:%M"),
                "note": entry.note,
            }
        )
    return {"files": items, "file_count": len(items), "total_bytes": total}


def purge_managed_copies(
    now: float | None = None, *, root: Path | None = None, dry_run: bool = False
) -> dict[str, int]:
    """执行登记副本清理；返回删除计数。I/O 真实失败向上传播（不伪报成功）。"""
    ts = time.time() if now is None else now
    root = root or _managed_copy_root()
    deleted = 0
    freed = 0
    dirs_removed = 0
    targets_seen: set[Path] = set()
    for _entry, f in _scan_managed_copies(ts, root):
        if dry_run:
            continue
        if _is_link_like(f):
            continue  # R-112 N01：删除前复核（纵深防护）
        try:
            size = f.stat().st_size
            f.unlink()
        except FileNotFoundError:
            continue
        deleted += 1
        freed += size
        for parent in f.parents:
            if parent == root:
                break
            targets_seen.add(parent)
    if not dry_run:
        for d in sorted(targets_seen, key=lambda p: len(p.parts), reverse=True):
            if _is_link_like(d):
                continue  # R-112 N01：只回收真实空目录，不触碰链接
            try:
                d.rmdir()  # 仅空目录
                dirs_removed += 1
            except OSError:
                pass
    return {
        "managed_copy_files_deleted": deleted,
        "managed_copy_bytes_freed": freed,
        "managed_copy_dirs_removed": dirs_removed,
    }


async def purge_expired(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """执行保留期清理，返回各类清理行数。

    SQLite 不保留时区（读取为 naive UTC）， cutoff 一律使用 naive UTC 比较。
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    now = (now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)).replace(
        tzinfo=None
    )
    raw_cutoff = now - timedelta(days=settings.raw_retention_days)
    decision_cutoff = now - timedelta(days=settings.decision_retention_days)

    # 1) 事件去重记录：超原始期删除（幂等防线仅在窗口内有意义）
    r1 = await session.execute(
        delete(ProcessedEvent).where(ProcessedEvent.processed_at < raw_cutoff)
    )
    deleted_events = int(getattr(r1, "rowcount", 0) or 0)

    # 2) 违规记录：超原始期的消息快照置为已清理占位（保留元数据供统计）
    stmt = select(ViolationRecord).where(ViolationRecord.created_at < raw_cutoff)
    result = await session.execute(stmt)
    purged_snapshots = 0
    purged_violation_evidence = 0
    purged_case_reasons = 0
    for violation in result.scalars():
        if violation.message_snapshot_json != _PURGED_SNAPSHOT:
            violation.message_snapshot_json = _PURGED_SNAPSHOT
            purged_snapshots += 1
        # Only redact a case reason proven to be the exact copy written by
        # revoke_violation for this expired source. Do not erase unrelated/new
        # human audit notes merely because the case itself is old.
        if violation.case_id and violation.revoke_reason:
            case = await session.get(Case, violation.case_id)
            audit = _parse_json(case.audit_json, {}) if case else {}
            if isinstance(audit, dict) and audit.get("revoke_reason"):
                copied = f"{audit['revoke_reason']}（操作人: {audit.get('revoked_by', '')}）"
                if violation.revoke_reason == copied and audit["revoke_reason"] != _PURGED_REASON:
                    audit["revoke_reason"] = _PURGED_REASON
                    assert case is not None
                    case.audit_json = json.dumps(audit, ensure_ascii=False)
                    purged_case_reasons += 1
        hits = json.dumps(
            _metadata_items(_parse_json(violation.rule_hits_json, []), _RULE_METADATA),
            ensure_ascii=False,
            sort_keys=True,
        )
        reason = _PURGED_REASON if violation.revoke_reason else ""
        if (violation.rule_hits_json, violation.revoke_reason) != (hits, reason):
            violation.rule_hits_json = hits
            violation.revoke_reason = reason
            purged_violation_evidence += 1

    copy_counts = await _purge_content_copies(session, raw_cutoff, now)

    # 3) 动作日志：超判断期删除
    r3 = await session.execute(delete(ActionLog).where(ActionLog.created_at < decision_cutoff))
    deleted_logs = int(getattr(r3, "rowcount", 0) or 0)

    # 4) 媒体文件清理（R-102-4）：与原始期一致，超保留期删除
    from app.adapters.qq_official.media import purge_media
    from app.runtime.pipeline import MEDIA_DIR

    deleted_media = purge_media(MEDIA_DIR, settings.raw_retention_days)

    # 4b) 登记式副本清理（"15 天全副本工程"）：只删登记路径中的到期文件；
    # 未知目录永不触碰（D-026）。
    managed_copy_counts = purge_managed_copies(now.replace(tzinfo=UTC).timestamp())

    # 5) OneBot durable inbox also contains original message content.
    from app.runtime.inbox import purge_inbox

    inbox_counts = await purge_inbox(
        session,
        now=now,
        raw_retention_days=settings.raw_retention_days,
        decision_retention_days=settings.decision_retention_days,
    )

    from app.notifications.service import purge_notifications

    notification_counts = await purge_notifications(session, before=decision_cutoff)

    # 6) Archive lifecycle is separate from raw-content expiry.
    case_counts = await _purge_case_lifecycle(session, now=now, raw_cutoff=raw_cutoff)

    # 7) AI 调用明细元数据（含群/消息标识）：90 天删除，不含长期汇总承诺。
    from app.moderation.ai import AIUsageLog as _AIUsageLog

    ai_log_cutoff = now - timedelta(days=90)
    r_ai = await session.execute(
        delete(_AIUsageLog)
        .where(_AIUsageLog.created_at < ai_log_cutoff)
        .execution_options(synchronize_session=False)
    )
    ai_logs_deleted = int(getattr(r_ai, "rowcount", 0) or 0)

    # 8) 候选规则过期（R-103 评审管理）：PROPOSED 超 30 天未处理 → DISMISSED
    from app.moderation.feedback import RuleCandidate as _RuleCandidate

    cand_cutoff = now - timedelta(days=30)
    candidates_expired = 0
    for candidate in await session.scalars(
        select(_RuleCandidate).where(
            _RuleCandidate.status == "PROPOSED", _RuleCandidate.created_at < cand_cutoff
        )
    ):
        report = _parse_json(candidate.replay_report_json, {})
        report = report if isinstance(report, dict) else {}
        report["invalidation_reason"] = "expired_30d_unhandled"
        candidate.status = "DISMISSED"
        candidate.replay_report_json = json.dumps(report, ensure_ascii=False)
        candidates_expired += 1

    await session.commit()
    return {
        "processed_events_deleted": deleted_events,
        "violation_snapshots_purged": purged_snapshots,
        "violation_evidence_purged": purged_violation_evidence,
        "case_reasons_purged": purged_case_reasons,
        "action_logs_deleted": deleted_logs,
        "media_files_deleted": deleted_media,
        **managed_copy_counts,
        **case_counts,
        "ai_usage_logs_deleted": ai_logs_deleted,
        "candidates_expired": candidates_expired,
        **copy_counts,
        **inbox_counts,
        **{f"notification_{key}": count for key, count in notification_counts.items()},
    }
