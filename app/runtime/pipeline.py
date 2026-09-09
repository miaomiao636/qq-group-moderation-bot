"""影子模式流水线（T-403，R-102 整改）。

处理顺序：begin_processing → 解析 → 文字规则 → 复核门 → 按媒体类型分发对应引擎 → 落库 → mark_processed。
任何异常 → mark_failed（可重试）；媒体缺失/下载失败/解析失败 → record_only，绝不允许放行。
影子模式：**任何判定都不执行**撤回/禁言/警告。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.orchestrator import (
    OfficialActionClient,
    orchestrate_actions,
    summarize_intents,
)
from app.core.contracts import MessageParseError, MessageSource, StandardMessage
from app.core.dedup import begin_processing, mark_failed, mark_processed
from app.moderation.ai import AIReviewService
from app.moderation.decision import ModerationDecision
from app.moderation.dynamic_rules import load_cached_active_snapshot
from app.moderation.image_engine import ImageModerationEngine, MediaAnalysis, merge_decisions
from app.moderation.media_engine import evaluate_file, evaluate_video, evaluate_voice
from app.moderation.review_gate import ReviewGate
from app.moderation.rules import TextRuleEngine
from app.runtime.models import ShadowDecision

MEDIA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "media"
logger = logging.getLogger(__name__)

PayloadPreprocessor = Callable[[dict[str, Any]], Awaitable[None]]


def _best_effort_identity(payload: dict[str, Any]) -> tuple[str, str, str]:
    """解析失败时的兜底身份提取（兼容官方与 OneBot 常见键名）。

    仅用于 record_only 人工记录，绝不用于处罚决策。
    """
    author = payload.get("author") or {}
    if not isinstance(author, dict):
        author = {}
    sender = payload.get("sender") or {}
    if not isinstance(sender, dict):
        sender = {}
    group = str(
        payload.get("group_openid")
        or payload.get("external_group_id")
        or payload.get("group_id")
        or ""
    )
    user = str(
        author.get("member_openid")
        or author.get("user_id")
        or sender.get("member_openid")
        or sender.get("user_id")
        or payload.get("user_id")
        or ""
    )
    name = str(author.get("username") or sender.get("nickname") or sender.get("card") or "")
    return group, user, name


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


def _safe_media_path(filename: str) -> Path | None:
    """把附件名约束在 ``MEDIA_DIR`` 顶层，拒绝绝对路径、穿越和逃逸符号链接。"""
    if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
        return None
    root = MEDIA_DIR.resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root:
        return None
    return candidate


async def run_pipeline(
    payload: dict[str, Any],
    session: AsyncSession,
    *,
    text_engine: TextRuleEngine | None = None,
    image_engine: ImageModerationEngine | None = None,
    ai_service: AIReviewService | None = None,
    official_action_client: OfficialActionClient | None = None,
    message_source: MessageSource | None = None,
    dedup_key: str | None = None,
    prepare_payload: PayloadPreprocessor | None = None,
) -> ShadowDecision | None:
    """处理一条群消息事件载荷。

    T-305：``message_source`` 为传输中立入站 seam；缺省使用QQ官方解析器
    （行为与历史版本一致）。审核/落库/动作链路对 source 一视同仁。
    T-306：``dedup_key`` 允许入站Adapter使用 ``provider + self_id +
    message_id`` 等更完整的持久化去重键（缺省仍为消息ID）。

    返回 ShadowDecision：成功处理（含解析失败/媒体缺失的 record_only 记录）；
    返回 None：事件已被去重跳过。
    """
    message_id = str(payload.get("id") or payload.get("message_id") or "")
    if not message_id:
        return None
    claim_key = dedup_key or message_id
    provider_name = str(message_source.provider) if message_source is not None else None
    claim = await begin_processing(session, claim_key, provider=provider_name)
    if not claim.accepted:
        return None

    image_engine = image_engine or ImageModerationEngine()
    gate = ReviewGate()
    if message_source is None:
        from app.runtime.official_wiring import build_official_message_source

        message_source = build_official_message_source()
    provider = str(message_source.provider)

    try:
        # 下载等有副作用的准备工作必须在持久化去重认领之后执行，避免重放
        # 事件重复下载同一媒体。
        if prepare_payload is not None:
            await prepare_payload(payload)
        msg: StandardMessage = message_source.parse_group_message(payload)
    except MessageParseError as exc:
        # 永久契约解析失败只落一条 record_only，重复投递不再自动重试。
        fallback_group, fallback_user, fallback_name = _best_effort_identity(payload)
        record = await upsert_shadow_decision(
            session,
            message_id=claim_key,
            external_message_id=message_id,
            group_openid=fallback_group,
            member_openid=fallback_user,
            sender_name=fallback_name[:64],
            provider=provider,
            external_group_id=fallback_group,
            external_user_id=fallback_user,
            kind="unknown",
            verdict="record_only",
            reason=f"事件解析失败，转人工：{exc}"[:500],
            detail_json=json.dumps({"parse_error": str(exc)}, ensure_ascii=False),
        )
        await mark_failed(
            session,
            claim_key,
            claim.token,
            f"MessageParseError: {exc}",
            error_kind="permanent",
        )
        return record
    except Exception as exc:  # noqa: BLE001 - 准备阶段失败必须可重试
        logger.exception("pipeline preparation failed for %s", message_id)
        await session.rollback()
        await mark_failed(session, claim_key, claim.token, f"{type(exc).__name__}: {exc}")
        return None

    # 按群审核开关：禁用群的消息仅记录为allow，不进入规则/AI/媒体审核
    from app.core.group_settings import is_moderation_enabled

    if not await is_moderation_enabled(session, msg.external_group_id):
        record = await upsert_shadow_decision(
            session,
            message_id=claim_key,
            external_message_id=msg.external_message_id,
            group_openid=msg.external_group_id,
            member_openid=msg.external_user_id,
            provider=msg.provider,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            sender_name=(msg.sender.username or "")[:64],
            kind=msg.kind,
            verdict="allow",
            reason="群审核已禁用（管理员设置），仅记录",
            detail_json=json.dumps({"moderation_disabled": True}, ensure_ascii=False),
        )
        await mark_processed(session, claim_key, claim.token)
        return record

    try:
        rule_version_ids: tuple[int, ...] = ()
        local_ai_media_paths: list[Path] = []
        rule_snapshot = await load_cached_active_snapshot(session, msg.external_group_id)
        rule_version_ids = rule_snapshot.version_ids
        if text_engine is None:
            text_engine = TextRuleEngine(rule_snapshot=rule_snapshot)
        else:
            text_engine.set_rule_snapshot(rule_snapshot)
        decision: ModerationDecision = text_engine.evaluate(msg)
        decision = gate.review(msg, decision)

        # T-306：无法解析的内容（未知消息段/合并转发骨架）绝不判正常，
        # 也绝不作为处罚依据——强制降级人工复核。
        if _contains_unreviewable_content(msg):
            decision = decision.model_copy(
                update={
                    "verdict": "record_only",
                    "reason": (decision.reason + "；" if decision.reason else "")
                    + "包含无法解析的内容（未知消息段/合并转发），转人工",
                }
            )

        # R-102-1 按媒体类型分发对应引擎；R-102-3 媒体缺失/下载失败 → record_only
        if msg.attachments:
            media_decisions: list[ModerationDecision] = []
            media_missing = False
            for att in msg.attachments:
                local = _safe_media_path(att.filename)
                if not local or not local.exists():
                    media_missing = True
                    continue
                if _is_image(att.content_type):
                    local_ai_media_paths.append(local)
                if _is_voice(att.content_type):
                    media_decisions.append(
                        evaluate_voice(
                            message_id,
                            msg.external_group_id,
                            msg.external_user_id,
                            att,
                            text_engine,
                        )
                    )
                elif _is_video(att.content_type):
                    media_decisions.append(
                        evaluate_video(
                            message_id,
                            msg.external_group_id,
                            msg.external_user_id,
                            local,
                            image_engine,
                            MEDIA_DIR / "_frames",
                        )
                    )
                elif _is_file(att.content_type):
                    media_decisions.append(
                        evaluate_file(
                            message_id,
                            msg.external_group_id,
                            msg.external_user_id,
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
        if ai_service is None:
            from app.runtime.ai_wiring import build_default_ai_review_service

            ai_service = build_default_ai_review_service()
        service = ai_service
        decision, ai_results = await service.review_message(
            session,
            msg,
            decision,
            media_paths=local_ai_media_paths,
            rule_version_ids=rule_version_ids,
        )
        detail = {
            "external_message_id": msg.external_message_id,
            "rule_hits": [h.model_dump() for h in decision.rule_hits],
            "recommended_actions": list(decision.recommended_actions),
            "is_protected_sender": decision.is_protected_sender,
            "text_preview": msg.text[:60],
            "media_kinds": [a.content_type for a in msg.attachments],
            # T-306增强：媒体文件名（SHA安全名），供后台详情页回看原图/视频
            "media_files": [{"name": a.filename, "type": a.content_type} for a in msg.attachments],
            "rule_version_ids": list(rule_version_ids),
            "ai_results": [r.model_dump() for r in ai_results],
        }
        if msg.segments:
            # T-306：中立段摘要（含未知段元数据），供人工复核追溯
            detail["segments"] = [
                {"kind": s.kind, "text": s.text[:80], "attachment_index": s.attachment_index}
                for s in msg.segments
            ]
        record = await upsert_shadow_decision(
            session,
            message_id=claim_key,
            external_message_id=msg.external_message_id,
            group_openid=msg.external_group_id,
            member_openid=msg.external_user_id,
            provider=msg.provider,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
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
        await mark_processed(session, claim_key, claim.token)
        return record
    except Exception as exc:  # noqa: BLE001 - 任何处理异常必须可重试
        logger.exception("pipeline processing failed for %s", message_id)
        await session.rollback()
        await mark_failed(session, claim_key, claim.token, f"{type(exc).__name__}: {exc}")
        return None


def _contains_unreviewable_content(msg: StandardMessage) -> bool:
    """消息是否包含无法自动判定的内容（T-306：未知段/合并转发）。"""
    if msg.kind in ("unknown", "forward_record"):
        return True
    return any(s.kind in ("unknown", "forward_record") for s in msg.segments)


def _media_decision_from(
    m: MediaAnalysis, message_id: str, msg: StandardMessage
) -> ModerationDecision:
    """把图片引擎 MediaAnalysis 转成 ModerationDecision 以便统一聚合。"""
    if m.verdict == "violation_high":
        return ModerationDecision(
            message_id=message_id,
            provider=msg.provider,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            verdict="violation_high",
            category="ad",
            confidence=m.confidence,
            reason=m.reason,
        )
    if m.verdict == "allow":
        return ModerationDecision(
            message_id=message_id,
            provider=msg.provider,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            verdict="allow",
            confidence=m.confidence,
            reason=m.reason,
        )
    return ModerationDecision(
        message_id=message_id,
        provider=msg.provider,
        external_group_id=msg.external_group_id,
        external_user_id=msg.external_user_id,
        verdict="record_only",
        confidence=m.confidence,
        reason=m.reason,
    )


async def upsert_shadow_decision(session: AsyncSession, **values: Any) -> ShadowDecision:
    """按内部事件键 ``message_id`` 幂等写入影子判定记录。"""
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
