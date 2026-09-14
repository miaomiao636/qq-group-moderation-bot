"""校园墙紧邻文字配对豁免（负责人 2026-09-14 口径）。

场景：成员先发一张带校园墙特征的图片（绝对放行），紧接着发与图内文案相似的
文字（即使文字命中广告/引流规则）。负责人决定：此类紧邻文字**不自动撤回**。

规则（按负责人指定参数）：
- 窗口：文字与图片落库时间差 <= 120 秒（2 分钟），且图片在前；
- 相似度：difflib.SequenceMatcher >= 60%；
- 配对：每张图只豁免**紧邻的一条**文字（消费后回写标记，1:1）；
- 豁免语义：不撤回、不立案——verdict 降为 record_only 并清空动作建议；
- 范围：仅 ad（广告/引流）类 violation_high 触发；fraud/porn 等不豁免。

图内文案来源：t204-v11 起要求 AI 放行校园墙图时在 evidence 中按
「校园墙白名单|文案:<图内文字>」格式摘录；旧格式（无文案）退化为不豁免。
"""

from __future__ import annotations

import difflib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.moderation.decision import ModerationDecision
from app.runtime.models import ShadowDecision

WALL_PAIR_WINDOW_SECONDS = 120
WALL_PAIR_SIMILARITY = 0.60
WALL_MARK = "校园墙白名单"
WALL_TEXT_MARK = "文案:"
PAIRED_KEY = "wall_paired"


def extract_wall_text(evidence: str) -> str | None:
    """从 vision evidence 提取图内文案。

    返回 None 表示这不是校园墙放行图；返回空串表示是校园墙图但无文案（旧格式，
    不参与豁免）；否则返回摘录文案。
    """
    if not evidence or WALL_MARK not in evidence:
        return None
    if WALL_TEXT_MARK in evidence:
        return evidence.split(WALL_TEXT_MARK, 1)[1].strip()
    return ""


def similarity(a: str, b: str) -> float:
    """归一化相似度（SequenceMatcher，忽略大小写与空白差异）。"""
    if not a or not b:
        return 0.0
    norm = lambda s: "".join(str(s).lower().split())  # noqa: E731
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def _wall_text_from_detail(detail_json: str) -> str | None:
    """从影子判定 detail 的 ai_results 中提取校园墙图内文案。"""
    try:
        detail = json.loads(detail_json)
    except (TypeError, ValueError):
        return None
    if detail.get(PAIRED_KEY):
        return None  # 该图已消费过豁免名额（1:1 配对）
    best: str | None = None
    for r in detail.get("ai_results") or []:
        if (r.get("source") or "") != "vision":
            continue
        text = extract_wall_text(str(r.get("evidence") or ""))
        if text is None:
            continue
        if best is None or len(text) > len(best):
            best = text
    return best


async def maybe_wall_text_pairing(
    session: AsyncSession, msg, decision: ModerationDecision
) -> ModerationDecision:
    """紧邻校园墙图的相似文字豁免（见模块 docstring 的负责人口径）。

    必须在 orchestrate_actions 之前调用；命中时返回 record_only 版本并
    消耗配对图的名额（回写 wall_paired，随流水线事务提交）。
    """
    if decision.verdict != "violation_high" or decision.category != "ad":
        return decision
    if msg.kind != "text" or not msg.text.strip():
        return decision
    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(seconds=WALL_PAIR_WINDOW_SECONDS)
    rows = (
        (
            await session.execute(
                select(ShadowDecision)
                .where(
                    ShadowDecision.provider == msg.provider,
                    ShadowDecision.external_group_id == msg.external_group_id,
                    ShadowDecision.external_user_id == msg.external_user_id,
                    ShadowDecision.kind == "image",
                    ShadowDecision.verdict.in_(("allow", "record_only")),
                    ShadowDecision.created_at >= cutoff,
                    ShadowDecision.created_at <= now,
                )
                .order_by(ShadowDecision.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        wall_text = _wall_text_from_detail(row.detail_json or "{}")
        if not wall_text:
            continue
        ratio = similarity(wall_text, msg.text)
        if ratio < WALL_PAIR_SIMILARITY:
            continue
        # 命中：消耗该图名额并豁免本条文字
        try:
            detail = json.loads(row.detail_json)
        except (TypeError, ValueError):
            detail = {}
        detail[PAIRED_KEY] = True
        row.detail_json = json.dumps(detail, ensure_ascii=False)
        return decision.model_copy(
            update={
                "verdict": "record_only",
                "recommended_actions": [],
                "reason": (
                    f"校园墙紧邻文字豁免：与{WALL_PAIR_WINDOW_SECONDS // 60}分钟内"
                    f"同成员校园墙图文案相似度{ratio:.0%}（负责人 2026-09-14 口径），"
                    "不撤回，转记录"
                ),
            }
        )
    return decision
