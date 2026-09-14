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
from datetime import UTC, datetime, timedelta
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


async def purge_expired(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """执行保留期清理，返回各类清理行数。

    SQLite 不保留时区（读取为 naive UTC）， cutoff 一律使用 naive UTC 比较。
    """
    settings = get_settings()
    now = (now or datetime.now(UTC)).replace(tzinfo=None)
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

    # 6) 案件生命周期（负责人 2026-09-14 口径）：
    #    终态(CLOSED) 满 15 天 → 归档（列表隐藏、数据保留）；
    #    归档满 3 个月且原文已脱敏（15 天保留期已完成）→ 合规清除（连带其违规记录——
    #    此时窗口早已关闭，不会再有其他案件引用；编号/通知游标契约不受影响：
    #    只删最老案件，不会触发当日编号冲突，也不会把 rowid 拉回到通知游标之下）。
    from datetime import timedelta as _td

    from app.cases.models import Case as _Case
    from app.cases.models import ViolationRecord as _ViolationRecord

    archive_cutoff = now - _td(days=15)
    archive_cutoff_naive = archive_cutoff.replace(tzinfo=None)
    closed_states = ("CLOSED",)
    archive_base = (
        update(_Case)
        .where(
            _Case.archived.is_(False),
            _Case.status.in_(closed_states),
            _Case.closed_at.is_not(None),
            _Case.closed_at < archive_cutoff_naive,
        )
        .values(archived=True, archived_at=now)
        .execution_options(synchronize_session=False)
    )
    r_arch = await session.execute(archive_base)
    cases_archived = int(getattr(r_arch, "rowcount", 0) or 0)

    purge_cutoff = (now - _td(days=90)).replace(tzinfo=None)
    old_cases = (
        await session.execute(
            select(_Case.id, _Case.violation_ids_json).where(
                _Case.archived.is_(True),
                _Case.archived_at.is_not(None),
                _Case.archived_at < purge_cutoff,
            )
        )
    ).all()
    cases_purged = 0
    records_purged = 0
    for _cid, _vids_json in old_cases:
        # R03 主审实测：SQLite rowid 会复用已删最大 ID——若物理删除案件，
        # 新案件 ID 可能回退到通知游标之下，导致新案件永远不发通知。
        # 因此「清除」= 逻辑清除：案件壳与 ID 保留（游标/编号安全），
        # 仅解除违规引用并清空内容字段；对应违规记录行物理删除
        # （其 ID 无游标依赖，且 90 天后已无任何案件引用，30 天合并窗口早已关闭）。
        try:
            vid_list = [int(x) for x in json.loads(_vids_json)]
        except (TypeError, ValueError):
            vid_list = []
        for vid in vid_list:
            r_del_v = await session.execute(
                delete(_ViolationRecord)
                .where(_ViolationRecord.id == vid)
                .execution_options(synchronize_session=False)
            )
            records_purged += int(getattr(r_del_v, "rowcount", 0) or 0)
        r_clr = await session.execute(
            update(_Case)
            .where(_Case.id == _cid)
            .values(violation_ids_json="[]", audit_json="{}")
            .execution_options(synchronize_session=False)
        )
        cases_purged += int(getattr(r_clr, "rowcount", 0) or 0)

    # 7) AI 用量日志（脱敏纯统计）：90 天清理（负责人 2026-09-14 决定）
    from app.moderation.ai import AIUsageLog as _AIUsageLog

    ai_log_cutoff = (now - _td(days=90)).replace(tzinfo=None)
    r_ai = await session.execute(
        delete(_AIUsageLog)
        .where(_AIUsageLog.created_at < ai_log_cutoff)
        .execution_options(synchronize_session=False)
    )
    ai_logs_deleted = int(getattr(r_ai, "rowcount", 0) or 0)

    # 8) 候选规则过期（R-103 评审管理）：PROPOSED 超 30 天未处理 → DISMISSED
    from app.moderation.feedback import RuleCandidate as _RuleCandidate

    cand_cutoff = (now - _td(days=30)).replace(tzinfo=None)
    r_cand = await session.execute(
        update(_RuleCandidate)
        .where(
            _RuleCandidate.status == "PROPOSED",
            _RuleCandidate.created_at < cand_cutoff,
        )
        .values(
            status="DISMISSED",
            replay_report_json='{"invalidation_reason": "expired_30d_unhandled"}',
        )
        .execution_options(synchronize_session=False)
    )
    candidates_expired = int(getattr(r_cand, "rowcount", 0) or 0)

    await session.commit()
    return {
        "processed_events_deleted": deleted_events,
        "violation_snapshots_purged": purged_snapshots,
        "violation_evidence_purged": purged_violation_evidence,
        "case_reasons_purged": purged_case_reasons,
        "action_logs_deleted": deleted_logs,
        "media_files_deleted": deleted_media,
        "cases_archived": cases_archived,
        "cases_purged": cases_purged,
        "violation_records_purged": records_purged,
        "ai_usage_logs_deleted": ai_logs_deleted,
        "candidates_expired": candidates_expired,
        **copy_counts,
        **inbox_counts,
        **{f"notification_{key}": count for key, count in notification_counts.items()},
    }
