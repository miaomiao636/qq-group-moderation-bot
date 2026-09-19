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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.orchestrator import (
    OfficialActionClient,
    orchestrate_actions,
    summarize_intents,
)
from app.core.contracts import MessageParseError, MessageSource, StandardMessage
from app.core.dedup import begin_processing, mark_failed, mark_processed
from app.moderation.ai import AIReviewService
from app.moderation.allowlist import load_allowlist_members, load_allowlist_terms
from app.moderation.decision import (
    ALLOWLIST_MEMBER_ALLOW_RULE_ID,
    POLICY_ALLOW_RULE_IDS,
    ModerationDecision,
)
from app.moderation.dynamic_rules import load_cached_active_snapshot
from app.moderation.image_engine import ImageModerationEngine, MediaAnalysis, merge_decisions
from app.moderation.image_hash import REVIEWED_MAX_DISTANCE, observe_shadow
from app.moderation.image_hash import mode as image_hash_mode
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
    """Run one event; active ordering metadata is removed on return/cancellation.

    Durable recovery remains the inbox/lease contract. Shadow rows are still first
    inserted only for completed judgments, so notification cursors remain valid.
    """
    from app.runtime.pairing_context import discard_active_pairing_message

    progress_token = object()
    try:
        return await _run_pipeline(
            payload,
            session,
            text_engine=text_engine,
            image_engine=image_engine,
            ai_service=ai_service,
            official_action_client=official_action_client,
            message_source=message_source,
            dedup_key=dedup_key,
            prepare_payload=prepare_payload,
            progress_token=progress_token,
        )
    finally:
        # A rejected duplicate must not unregister another worker's active event.
        discard_active_pairing_message(progress_token)


async def _run_pipeline(
    payload: dict[str, Any],
    session: AsyncSession,
    *,
    text_engine: TextRuleEngine | None,
    image_engine: ImageModerationEngine | None,
    ai_service: AIReviewService | None,
    official_action_client: OfficialActionClient | None,
    message_source: MessageSource | None,
    dedup_key: str | None,
    prepare_payload: PayloadPreprocessor | None,
    progress_token: object,
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
        # 先做不含 I/O 的解析：媒体下载/AI 可能慢于后续文字。活跃 worker 只登记
        # 最小排序元数据，不提前插入影子判定；未启动 worker 的前驱从 Inbox 查询。
        msg: StandardMessage = message_source.parse_group_message(payload)
        from app.core.group_settings import is_moderation_enabled
        from app.runtime.pairing_context import register_active_pairing_message

        if not await is_moderation_enabled(session, msg.external_group_id, provider=msg.provider):
            await mark_processed(session, claim_key, claim.token)
            return None
        register_active_pairing_message(progress_token, msg, claim_key)
        # 下载等有副作用的准备工作必须在持久化去重认领之后执行，避免重放
        # 事件重复下载同一媒体。
        if prepare_payload is not None:
            await prepare_payload(payload)
            msg = message_source.parse_group_message(payload)
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

    from app.core.group_settings import ambiguous_legacy_rule_scope

    try:
        rule_version_ids: tuple[int, ...] = ()
        local_ai_media_paths: list[Path] = []
        evidence_vetoes: list[str] = []
        ambiguous_scope = await ambiguous_legacy_rule_scope(
            session, msg.external_group_id, msg.provider
        )
        rule_snapshot = await load_cached_active_snapshot(
            session, None if ambiguous_scope else msg.external_group_id
        )
        rule_version_ids = rule_snapshot.version_ids
        # 全局白名单每消息直读（跨进程即时生效；读取失败 fail-closed 为空集）。
        allow_terms = await load_allowlist_terms(session)
        # 成员白名单（负责人 2026-09-18，按 QQ 号）：同样每消息直读、fail-closed。
        allow_members = await load_allowlist_members(session)
        if text_engine is None:
            text_engine = TextRuleEngine(rule_snapshot=rule_snapshot, allow_members=allow_members)
        else:
            text_engine.set_rule_snapshot(rule_snapshot)
        text_engine.set_allowlist(allow_terms)
        text_engine.set_allowlist_members(allow_members)
        decision: ModerationDecision = text_engine.evaluate(msg)
        decision = gate.review(msg, decision)
        # 成员白名单身份（D-037，负责人"名单内成员发的所有信息都通过"）：
        # "内容不可判定"类健康信号**不得**把它降级为转人工（主审 F07）。
        # 只抑制"我们看不懂这条内容"类 veto；配置/一致性异常（如同群多 provider 歧义）
        # 仍照常记录——不能把全部 evidence_veto 一概关闭（那会扩大到非名单成员）。
        member_policy_allow = decision.verdict == "allow" and any(
            h.rule_id == ALLOWLIST_MEMBER_ALLOW_RULE_ID for h in decision.rule_hits
        )

        # T-306：无法解析的内容（未知消息段）绝不判正常，也绝不作为处罚依据——
        # 强制降级人工复核。2026-09-18：**合并转发不再走此兜底**——负责人明确
        # 口径为"合并转发一律撤回"，由规则引擎 R_FORWARD_RECORD 直接给出 violation_high。
        if _contains_unreviewable_content(msg) and not member_policy_allow:
            evidence_vetoes.append("包含无法解析的内容（未知消息段）")
            decision = decision.model_copy(
                update={
                    "verdict": "record_only",
                    "reason": (decision.reason + "；" if decision.reason else "")
                    + "包含无法解析的内容（未知消息段），转人工",
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
                if (
                    not member_policy_allow
                    and not _is_image(att.content_type)
                    and media_decisions[-1].verdict == "record_only"
                ):
                    # A vision call on another image cannot review this voice,
                    # video or document. Preserve the incomplete-evidence gate.
                    evidence_vetoes.append("包含尚未完成审核的语音/视频/文件")

            if media_missing and not member_policy_allow:
                evidence_vetoes.append("媒体缺失/下载失败")
            if any(d.verdict == "violation_high" for d in media_decisions):
                if member_policy_allow:
                    # D-037（负责人 2026-09-18："名单内成员发的所有信息都通过"）：
                    # 成员白名单为**全类别完全放行**，媒体层独立违规信号只作证据，
                    # 不升级、不处罚。若不拦截，merge_decisions 会把 allow 直接改成
                    # violation_high，编排层随即产生真实撤回/禁言——等于白名单被旁路
                    # （与保护角色同样免罚的待遇一致）。
                    decision = decision.model_copy(
                        update={
                            "reason": (decision.reason + "；" if decision.reason else "")
                            + "媒体层检出违规信号，按成员白名单完全放行（D-037）仅记录",
                        }
                    )
                else:
                    decision = merge_decisions(
                        decision,
                        MediaAnalysis("violation_high", 0.95, reason="媒体违规"),
                    )
            elif media_missing and not member_policy_allow:
                # R-102-3 任意媒体缺失/下载失败 → 不放行
                # （成员白名单身份 D-037 例外：身份确定即可放行，主审 F07）
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
                and not any(h.rule_id in POLICY_ALLOW_RULE_IDS for h in decision.rule_hits)
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
        if ambiguous_scope:
            evidence_vetoes.append("同群 ID 存在不同 provider，旧群级规则适用范围有歧义")
        if evidence_vetoes:
            decision = decision.model_copy(
                update={
                    "verdict": "record_only",
                    "recommended_actions": [],
                    "reason": "；".join(dict.fromkeys(evidence_vetoes)) + "，转人工",
                }
            )
        # 图后窗口豁免（负责人 2026-09-17 口径 C；2026-09-18 晚扩展为"文字与图片"）：
        # 2 分钟窗口内同成员在"视觉确认放行图"之后发的内容不撤回，降为 record_only
        # 转记录（不再要求相似度/紧邻/一图一条）；色情/暴力/刷屏不豁免。
        # 前置过滤与豁免函数共用 is_pairing_candidate，避免两处条件漂移。
        from app.moderation.wall_pair import is_pairing_candidate, maybe_wall_text_pairing
        from app.runtime.pairing_context import load_pending_pairing_messages

        pending_messages: tuple[StandardMessage, ...] = ()
        if is_pairing_candidate(msg, decision):
            pending_messages = await load_pending_pairing_messages(
                session, msg, event_key=claim_key
            )
        decision = await maybe_wall_text_pairing(
            session, msg, decision, pending_messages=pending_messages, event_key=claim_key
        )
        detail = {
            "external_message_id": msg.external_message_id,
            # R06（ad323b6 主审）：记录消息发送时间，供紧邻配对按真实发送顺序校验，
            # 不再依赖处理完成时间（并发 worker 下会颠倒先图后文）。
            "sent_at": (msg.sent_at.isoformat() if msg.sent_at else ""),
            "rule_hits": [h.model_dump() for h in decision.rule_hits],
            "recommended_actions": list(decision.recommended_actions),
            "is_protected_sender": decision.is_protected_sender,
            "text_preview": msg.text[:60],
            "media_kinds": [a.content_type for a in msg.attachments],
            # T-306增强：媒体文件名（SHA安全名），供后台详情页回看原图/视频
            "media_files": [{"name": a.filename, "type": a.content_type} for a in msg.attachments],
            "rule_version_ids": list(rule_version_ids),
            "ai_results": [r.model_dump() for r in ai_results],
            "evidence_vetoes": list(dict.fromkeys(evidence_vetoes)),
            # R6-01-R：把**本次判定使用的复核阈值/政策上下文**随记录落库——离线工具（回放/导出）
            # 必须按当时配置解释二审是否有效，不能拿函数默认值（0.60/0.90）当确定结论。
            # 旧记录没有这个键时，离线一律标 `unresolved_unknown`（如实 unknown，不猜）。
            "review_policy": {
                "primary_direct_threshold": float(getattr(ai_service, "direct_threshold", 0.90)),
                "secondary_review_low": float(getattr(ai_service, "secondary_review_low", 0.60)),
                "secondary_review_high": float(getattr(ai_service, "secondary_review_high", 0.90)),
            },
        }
        if msg.segments:
            # T-306：中立段摘要（含未知段元数据），供人工复核追溯
            detail["segments"] = [
                {"kind": s.kind, "text": s.text[:80], "attachment_index": s.attachment_index}
                for s in msg.segments
            ]
        # 图片感知哈希白名单 **shadow 观察**（负责人 2026-09-19）：
        # `IMAGE_HASH_MODE=off`（默认）时完全不读白名单、不写字段——线上行为零变化；
        # `shadow` 时只把"是否命中 / 本可放行"写进判定明细，**不改变判定**（enforce 未实现）。
        # A06：把"全证据"层面的例外也算进来（严重类别 / 未定论 / 附件缺失 / 否决），
        # 否则 would_allow 会高估"本可放行"。
        # A03-R：**先判模式**——`off` / 非法值**不做任何观察专属 I/O**（连 stat 都不做，
        # 保证"默认 off 零运行影响"），且"准备 blockers + 观察"整体放在独立异常边界内：
        # 任何异常只记 `unavailable`，**不得**中断原判定、持久化与动作编排。
        observation_mode = image_hash_mode()
        image_hash_observation: dict[str, object] | None = None
        if observation_mode != "off":
            try:
                extra_blockers: list[str] = []
                if evidence_vetoes:
                    extra_blockers.append("evidence_veto")
                if any(str(r.category or "") in ("porn", "violence") for r in ai_results):
                    extra_blockers.append("attachment_category")
                # A06-R：未定论必须复用**已有**的附件复核判据（含二审异类/低置信/非独立模型/
                # 孤儿二审），并透传服务实际阈值——不再按两个布尔字段另写一套简化判断。
                from app.moderation.ai import _attachment_reviews_unresolved

                if _attachment_reviews_unresolved(
                    ai_results,
                    decision,
                    primary_direct_threshold=float(getattr(ai_service, "direct_threshold", 0.90)),
                    secondary_review_low=float(getattr(ai_service, "secondary_review_low", 0.60)),
                    secondary_review_high=float(getattr(ai_service, "secondary_review_high", 0.90)),
                ) or any(r.needs_review or r.degraded_reason for r in ai_results):
                    extra_blockers.append("unresolved")
                for att in msg.attachments:
                    if not str(att.content_type).startswith("image/"):
                        continue
                    try:
                        missing = not (MEDIA_DIR / Path(str(att.filename)).name).is_file()
                    except OSError:
                        missing = True
                    if missing:
                        extra_blockers.append("media_missing")
                        break
                image_hash_observation = await observe_shadow(
                    session,
                    attachments=msg.attachments,
                    media_dir=MEDIA_DIR,
                    verdict=decision.verdict,
                    category=decision.category or "",
                    rule_ids=[hit.rule_id for hit in decision.rule_hits],
                    max_distance=REVIEWED_MAX_DISTANCE,
                    additional_blockers=extra_blockers,
                )
            except Exception:  # noqa: BLE001 - 观察失败只记录，不影响审核
                logger.warning("图片哈希观察失败（已忽略，不影响判定）", exc_info=True)
                image_hash_observation = {
                    "mode": observation_mode,
                    "unavailable": "observer_error",
                }
        if image_hash_observation is not None:
            detail["image_hash"] = image_hash_observation
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
            record = await upsert_shadow_decision(
                session, message_id=claim_key, detail_json=json.dumps(detail, ensure_ascii=False)
            )
        await mark_processed(session, claim_key, claim.token)
        return record
    except Exception as exc:  # noqa: BLE001 - 任何处理异常必须可重试
        logger.exception("pipeline processing failed for %s", message_id)
        await session.rollback()
        await mark_failed(session, claim_key, claim.token, f"{type(exc).__name__}: {exc}")
        return None


def _contains_unreviewable_content(msg: StandardMessage) -> bool:
    """消息是否包含无法自动判定的内容（T-306：未知消息段）。

    2026-09-18（负责人口径）：``forward_record`` **不再**列入本兜底——合并转发由
    规则引擎 `R_FORWARD_RECORD` 判定为"一律撤回"，若在此强制 record_only 会把撤回
    降级掉。未知消息段仍然强制转人工（未被识别的结构不得作为处罚依据）。
    """
    if msg.kind == "unknown":
        return True
    return any(s.kind == "unknown" for s in msg.segments)


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
    """按内部事件键幂等落库（重试直接以最新 detail 覆盖）。

    口径 C（2026-09-17）起校园墙豁免为无状态重算：旧 wall_paired 绑定字段
    不再读写，图片重试无需保留任何配对元数据。
    """
    record = await session.scalar(
        select(ShadowDecision).where(ShadowDecision.message_id == values["message_id"])
    )
    if record is None:
        record = ShadowDecision(**values)
        session.add(record)
    else:
        await session.execute(
            update(ShadowDecision)
            .where(ShadowDecision.id == record.id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
    await session.commit()
    await session.refresh(record)
    return record
