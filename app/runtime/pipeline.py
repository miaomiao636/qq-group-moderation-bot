"""影子模式流水线（T-403，R-102 整改）。

处理顺序：begin_processing → 解析 → 文字规则 → 复核门 → 按媒体类型分发对应引擎 → 落库 → mark_processed。
任何异常 → mark_failed（可重试）；媒体缺失/下载失败/解析失败 → record_only，绝不允许放行。
影子模式：**任何判定都不执行**撤回/禁言/警告。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.orchestrator import (
    OfficialActionClient,
    orchestrate_actions,
    summarize_intents,
)
from app.adapters.qq_official.contract import StandardMessage
from app.adapters.qq_official.dedup import begin_processing, mark_failed, mark_processed
from app.adapters.qq_official.parser import EventParseError, parse_group_message
from app.moderation.ai import AIReviewService, build_default_ai_review_service
from app.moderation.decision import ModerationDecision
from app.moderation.dynamic_rules import load_cached_active_snapshot
from app.moderation.image_engine import ImageModerationEngine, MediaAnalysis, merge_decisions
from app.moderation.media_engine import evaluate_file, evaluate_video, evaluate_voice
from app.moderation.review_gate import ReviewGate
from app.moderation.rules import TextRuleEngine
from app.runtime.models import ShadowDecision

MEDIA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "media"
logger = logging.getLogger(__name__)


def _is_image(content_type: str) -> bool:
    return content_type.startswith("image/")


def _is_voice(content_type: str) -> bool:
    return content_type == "voice" or content_type.startswith("audio/")


def _is_video(content_type: str) -> bool:
    return content_type.startswith("video/")


def _is_file(content_type: str) -> bool:
    return (
        content_type == "file"
        or content_type.startswith("application/")
        or content_type == "application/octet-stream"
    )


async def run_pipeline(
    payload: dict[str, Any],
    session: AsyncSession,
    *,
    text_engine: TextRuleEngine | None = None,
    image_engine: ImageModerationEngine | None = None,
    ai_service: AIReviewService | None = None,
    official_action_client: OfficialActionClient | None = None,
) -> ShadowDecision | None:
    """处理一条 GROUP_MESSAGE_CREATE 载荷。

    返回 ShadowDecision：成功处理（含解析失败/媒体缺失的 record_only 记录）；
    返回 None：事件已被去重跳过。
    """
    message_id = str(payload.get("id") or "")
    if not message_id:
        return None
    claim = await begin_processing(session, message_id)
    if not claim.accepted:
        return None

    image_engine = image_engine or ImageModerationEngine()
    gate = ReviewGate()

    try:
        msg: StandardMessage = parse_group_message(payload)
    except EventParseError as exc:
        # 永久契约解析失败只落一条 record_only，重复投递不再自动重试。
        record = await upsert_shadow_decision(
            session,
            message_id=message_id,
            group_openid=str(payload.get("group_openid") or ""),
            member_openid=str((payload.get("author") or {}).get("member_openid") or ""),
            sender_name=str((payload.get("author") or {}).get("username") or "")[:64],
            kind="unknown",
            verdict="record_only",
            reason=f"事件解析失败，转人工：{exc}"[:500],
            detail_json=json.dumps({"parse_error": str(exc)}, ensure_ascii=False),
        )
        await mark_failed(
            session,
            message_id,
            claim.token,
            f"EventParseError: {exc}",
            error_kind="permanent",
        )
        return record

    try:
        rule_version_ids: tuple[int, ...] = ()
        local_ai_media_paths: list[Path] = []
        rule_snapshot = await load_cached_active_snapshot(session, msg.group_openid)
        rule_version_ids = rule_snapshot.version_ids
        if text_engine is None:
            text_engine = TextRuleEngine(rule_snapshot=rule_snapshot)
        else:
            text_engine.set_rule_snapshot(rule_snapshot)
        decision: ModerationDecision = text_engine.evaluate(msg)
        decision = gate.review(msg, decision)

        # R-102-1 按媒体类型分发对应引擎；R-102-3 媒体缺失/下载失败 → record_only
        if msg.attachments:
            media_decisions: list[ModerationDecision] = []
            media_missing = False
            for att in msg.attachments:
                local = MEDIA_DIR / att.filename if att.filename else None
                if not local or not local.exists():
                    media_missing = True
                    continue
                if _is_image(att.content_type):
                    local_ai_media_paths.append(local)
                if _is_voice(att.content_type):
                    media_decisions.append(
                        evaluate_voice(
                            message_id, msg.group_openid, msg.sender.member_openid, att, text_engine
                        )
                    )
                elif _is_video(att.content_type):
                    media_decisions.append(
                        evaluate_video(
                            message_id,
                            msg.group_openid,
                            msg.sender.member_openid,
                            local,
                            image_engine,
                            MEDIA_DIR / "_frames",
                        )
                    )
                elif _is_file(att.content_type):
                    media_decisions.append(
                        evaluate_file(
                            message_id,
                            msg.group_openid,
                            msg.sender.member_openid,
                            local,
                            text_engine,
                        )
                    )
                else:
                    # image/gif/其他 → 图片引擎
                    m = image_engine.analyze(local)
                    media_decisions.append(_media_decision_from(m, message_id, msg))

            if any(d.verdict == "violation_high" for d in media_decisions):
                decision = merge_decisions(
                    decision,
                    MediaAnalysis("violation_high", 0.95, reason="媒体违规"),
                )
            elif media_missing:
                # R-102-3 任意媒体缺失/下载失败 → 不放行
                decision = decision.model_copy(
                    update={
                        "verdict": "record_only",
                        "reason": (
                            decision.reason + "；"
                            if decision.reason and decision.verdict == "record_only"
                            else ""
                        )
                        + "媒体缺失/下载失败，转人工",
                    }
                )
            elif (
                media_decisions
                and all(d.verdict == "allow" for d in media_decisions)
                and decision.verdict == "record_only"
                and not decision.rule_hits
            ):
                decision = decision.model_copy(
                    update={"verdict": "allow", "reason": "媒体全部放行"}
                )
            elif media_decisions and decision.verdict == "allow" and not decision.rule_hits:
                decision = decision.model_copy(
                    update={"verdict": "record_only", "reason": "媒体无法判定，转人工"}
                )
            elif (
                any(d.verdict == "record_only" for d in media_decisions)
                and decision.verdict == "allow"
            ):
                decision = decision.model_copy(
                    update={"verdict": "record_only", "reason": "媒体部分转人工"}
                )
        service = ai_service or build_default_ai_review_service()
        decision, ai_results = await service.review_message(
            session,
            msg,
            decision,
            media_paths=local_ai_media_paths,
            rule_version_ids=rule_version_ids,
        )
        detail = {
            "rule_hits": [h.model_dump() for h in decision.rule_hits],
            "recommended_actions": list(decision.recommended_actions),
            "is_protected_sender": decision.is_protected_sender,
            "text_preview": msg.text[:60],
            "media_kinds": [a.content_type for a in msg.attachments],
            "rule_version_ids": list(rule_version_ids),
            "ai_results": [r.model_dump() for r in ai_results],
        }
        record = await upsert_shadow_decision(
            session,
            message_id=msg.message_id,
            group_openid=msg.group_openid,
            member_openid=msg.sender.member_openid,
            sender_name=msg.sender.username[:64],
            kind=msg.kind,
            verdict=decision.verdict,
            category=decision.category or "",
            confidence=decision.confidence,
            reason=decision.reason[:500],
            detail_json=json.dumps(detail, ensure_ascii=False),
        )
        action_intents = await orchestrate_actions(
            session, msg, decision, official_client=official_action_client
        )
        if action_intents:
            detail["action_intents"] = summarize_intents(action_intents)
            record.detail_json = json.dumps(detail, ensure_ascii=False)
            await session.commit()
        await mark_processed(session, message_id, claim.token)
        return record
    except Exception as exc:  # noqa: BLE001 - 任何处理异常必须可重试
        logger.exception("pipeline processing failed for %s", message_id)
        await session.rollback()
        await mark_failed(session, message_id, claim.token, f"{type(exc).__name__}: {exc}")
        return None


def _media_decision_from(
    m: MediaAnalysis, message_id: str, msg: StandardMessage
) -> ModerationDecision:
    """把图片引擎 MediaAnalysis 转成 ModerationDecision 以便统一聚合。"""
    if m.verdict == "violation_high":
        return ModerationDecision(
            message_id=message_id,
            group_openid=msg.group_openid,
            sender_member_openid=msg.sender.member_openid,
            verdict="violation_high",
            category="ad",
            confidence=m.confidence,
            reason=m.reason,
        )
    if m.verdict == "allow":
        return ModerationDecision(
            message_id=message_id,
            group_openid=msg.group_openid,
            sender_member_openid=msg.sender.member_openid,
            verdict="allow",
            confidence=m.confidence,
            reason=m.reason,
        )
    return ModerationDecision(
        message_id=message_id,
        group_openid=msg.group_openid,
        sender_member_openid=msg.sender.member_openid,
        verdict="record_only",
        confidence=m.confidence,
        reason=m.reason,
    )


async def upsert_shadow_decision(session: AsyncSession, **values: Any) -> ShadowDecision:
    """按 message_id 幂等写入影子判定记录。"""
    record = await session.scalar(
        select(ShadowDecision).where(ShadowDecision.message_id == values["message_id"])
    )
    if record is None:
        record = ShadowDecision(**values)
        session.add(record)
    else:
        for key, value in values.items():
            setattr(record, key, value)
    await session.commit()
    return record
