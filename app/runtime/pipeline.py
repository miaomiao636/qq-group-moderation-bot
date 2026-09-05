"""影子模式流水线（T-403）：解析→去重→判定→记录，不执行任何动作。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.qq_official.contract import StandardMessage
from app.adapters.qq_official.dedup import check_and_mark
from app.adapters.qq_official.parser import EventParseError, parse_group_message
from app.moderation.decision import ModerationDecision
from app.moderation.image_engine import ImageModerationEngine, MediaAnalysis, merge_decisions
from app.moderation.review_gate import ReviewGate
from app.moderation.rules import TextRuleEngine
from app.runtime.models import ShadowDecision

MEDIA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "media"


async def run_pipeline(
    payload: dict[str, Any],
    session: AsyncSession,
    *,
    text_engine: TextRuleEngine | None = None,
    image_engine: ImageModerationEngine | None = None,
) -> ShadowDecision | None:
    """处理一条 GROUP_MESSAGE_CREATE 载荷，落一条影子判定记录。

    返回 None 表示事件被去重或解析失败（解析失败另行记日志，由调用方处理）。
    影子模式：任何判定都**不执行**撤回/禁言/警告。
    """
    message_id = str(payload.get("id") or "")
    if not message_id:
        return None
    if not await check_and_mark(session, message_id):
        return None

    text_engine = text_engine or TextRuleEngine()
    image_engine = image_engine or ImageModerationEngine()
    gate = ReviewGate()

    try:
        msg: StandardMessage = parse_group_message(payload)
    except EventParseError:
        await session.rollback()
        return None

    decision: ModerationDecision = text_engine.evaluate(msg)
    decision = gate.review(msg, decision)

    # 媒体判定（附件已由下载层保存到 MEDIA_DIR，按文件名对应）
    if msg.attachments:
        media_results = []
        for att in msg.attachments:
            local = MEDIA_DIR / att.filename if att.filename else None
            if local and local.exists():
                media_results.append(image_engine.analyze(local))
        if media_results:
            if any(m.verdict == "violation_high" for m in media_results):
                worst_media = MediaAnalysis(
                    "violation_high",
                    0.95,
                    reason="媒体黑名单命中",
                )
                decision = merge_decisions(decision, worst_media)
            elif (
                all(m.verdict == "allow" for m in media_results)
                and decision.verdict == "record_only"
                and not decision.rule_hits
            ):
                decision = decision.model_copy(
                    update={"verdict": "allow", "reason": "媒体白名单放行"}
                )
            elif decision.verdict == "allow" and not decision.rule_hits:
                # 有媒体但无法判定（未知图/无匹配）：不自动放行，转人工
                decision = decision.model_copy(
                    update={"verdict": "record_only", "reason": "媒体无法判定，转人工"}
                )

    record = ShadowDecision(
        message_id=msg.message_id,
        group_openid=msg.group_openid,
        member_openid=msg.sender.member_openid,
        kind=msg.kind,
        verdict=decision.verdict,
        category=decision.category or "",
        confidence=decision.confidence,
        reason=decision.reason[:500],
        detail_json=json.dumps(
            {
                "rule_hits": [h.model_dump() for h in decision.rule_hits],
                "recommended_actions": list(decision.recommended_actions),
                "is_protected_sender": decision.is_protected_sender,
                "text_preview": msg.text[:60],
            },
            ensure_ascii=False,
        ),
    )
    session.add(record)
    await session.commit()
    return record
