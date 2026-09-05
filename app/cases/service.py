"""违规阶梯服务（T-104）。

阶梯（PROJECT_CONTEXT 既定，同群+同成员+30天有效违规窗口）：
- 第1次高置信违规：证据保存 + 建议撤回 + 禁言1小时 + 一次警告；
- 第2次高置信违规：证据保存 + 建议撤回 + 禁言24小时 + 不警告 + 生成 PENDING_REVIEW 案件（合并证据）；
- 任何情况下**不自动创建踢人任务**；踢人只能来自人工审批（T-301）。

误判撤销：revoke_violation 将记录标记 revoked 并从窗口剔除；案件内全部违规被撤销时，
案件走 FALSE_POSITIVE → STRIKE_REVOKED → CLOSED。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.qq_official.contract import StandardMessage
from app.cases.case_sm import validate_transition
from app.cases.models import Case, ViolationRecord
from app.moderation.decision import ModerationDecision

WINDOW_DAYS = 30
STRIKE1_MUTE_SECONDS = 3600
STRIKE2_MUTE_SECONDS = 24 * 3600


class PlannedAction:
    """建议动作（结构中永不存在 kick）。"""

    def __init__(self, action: str, **params: Any) -> None:
        if action not in ("recall", "mute", "warn"):
            raise ValueError(f"非法动作类型: {action}（踢人必须人工审批，禁止出现在计划中）")
        self.action = action
        self.params = params

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, **self.params}


class ViolationOutcome:
    def __init__(
        self,
        strike_no: int,
        violation: ViolationRecord,
        planned_actions: list[PlannedAction],
        case: Case | None,
    ) -> None:
        self.strike_no = strike_no
        self.violation = violation
        self.planned_actions = planned_actions
        self.case = case


def _snapshot(msg: StandardMessage, decision: ModerationDecision) -> dict[str, Any]:
    """证据快照：消息文本、附件元数据、命中规则、决策。"""
    return {
        "message_id": msg.message_id,
        "group_openid": msg.group_openid,
        "sender": msg.sender.model_dump(),
        "sent_at": msg.sent_at.isoformat() if msg.sent_at else None,
        "text": msg.text,
        "mentions": msg.mentions,
        "attachments": [a.model_dump() for a in msg.attachments],
        "share_card": msg.share_card.model_dump() if msg.share_card else None,
        "rule_hits": [h.model_dump() for h in decision.rule_hits],
        "confidence": decision.confidence,
        "category": decision.category,
    }


async def _count_cases_today(session: AsyncSession, prefix: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(Case).where(Case.case_no.like(f"{prefix}-%"))
    )
    return int(result.scalar_one())


async def _generate_case_no(session: AsyncSession) -> str:
    today = datetime.now(UTC).date()
    prefix = f"R{today:%Y%m%d}"
    count = await _count_cases_today(session, prefix)
    return f"{prefix}-{count + 1:02d}"


async def count_active_violations(
    session: AsyncSession, group_openid: str, member_openid: str
) -> int:
    """30 天窗口内的有效违规数（revoked 不计）。"""
    window_start = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
    stmt = (
        select(func.count())
        .select_from(ViolationRecord)
        .where(
            ViolationRecord.group_openid == group_openid,
            ViolationRecord.member_openid == member_openid,
            ViolationRecord.revoked.is_(False),
            ViolationRecord.created_at >= window_start,
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def record_violation(
    session: AsyncSession,
    msg: StandardMessage,
    decision: ModerationDecision,
) -> ViolationOutcome:
    """记录一次高置信违规并返回阶梯结果（含建议动作与可能的案件）。

    调用方负责：执行 planned_actions 中的动作并回写 action_result_json。
    """
    if decision.verdict != "violation_high":
        raise ValueError("只有 violation_high 决策才能进入违规阶梯")
    if decision.is_protected_sender or msg.sender.role in ("owner", "admin"):
        raise ValueError("保护角色（群主/管理员）不得进入处罚阶梯")

    existing = await count_active_violations(session, msg.group_openid, msg.sender.member_openid)
    strike_no = existing + 1

    violation = ViolationRecord(
        group_openid=msg.group_openid,
        member_openid=msg.sender.member_openid,
        message_id=msg.message_id,
        category=decision.category or "other",
        confidence=decision.confidence,
        rule_hits_json=json.dumps([h.model_dump() for h in decision.rule_hits], ensure_ascii=False),
        message_snapshot_json=json.dumps(_snapshot(msg, decision), ensure_ascii=False),
    )
    session.add(violation)
    await session.flush()  # 取得 violation.id

    planned: list[PlannedAction] = [PlannedAction("recall", message_id=msg.message_id)]
    case: Case | None = None
    if strike_no == 1:
        planned.append(
            PlannedAction(
                "mute", member_openid=msg.sender.member_openid, seconds=STRIKE1_MUTE_SECONDS
            )
        )
        planned.append(
            PlannedAction(
                "warn",
                reply_to_message_id=msg.message_id,
                text="警告：您发布的内容违反群规，请立即停止。再次违规将被禁言24小时并立案审核。",
            )
        )
    else:
        planned.append(
            PlannedAction(
                "mute", member_openid=msg.sender.member_openid, seconds=STRIKE2_MUTE_SECONDS
            )
        )
        case = await _create_case(session, msg, violation)

    violation.case_id = case.id if case else None
    await session.commit()
    return ViolationOutcome(
        strike_no=strike_no, violation=violation, planned_actions=planned, case=case
    )


async def _create_case(
    session: AsyncSession, msg: StandardMessage, violation: ViolationRecord
) -> Case:
    """第二次违规：合并证据生成 PENDING_REVIEW 案件。

    R-102-8：
    - 幂等：若同群同成员已有 PENDING_REVIEW 案件则复用之，避免并发违规产生重复案件；
    - 案件编号冲突重试：case_no 唯一约束冲突时重新生成。
    """
    # 幂等：已有 PENDING_REVIEW 案件则直接返回，不重复立案
    existing_stmt = select(Case).where(
        Case.group_openid == msg.group_openid,
        Case.member_openid == msg.sender.member_openid,
        Case.status == "PENDING_REVIEW",
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    # 合并窗口内全部有效违规证据
    window_start = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
    stmt = select(ViolationRecord).where(
        ViolationRecord.group_openid == msg.group_openid,
        ViolationRecord.member_openid == msg.sender.member_openid,
        ViolationRecord.revoked.is_(False),
        ViolationRecord.created_at >= window_start,
    )
    result = await session.execute(stmt)
    related = list(result.scalars())

    # case_no 冲突重试（最多5次，防止并发同日编号碰撞）
    for _attempt in range(5):
        case_no = await _generate_case_no(session)
        case = Case(
            case_no=case_no,
            group_openid=msg.group_openid,
            member_openid=msg.sender.member_openid,
            status="PENDING_REVIEW",
            violation_ids_json=json.dumps([v.id for v in related]),
            audit_json=json.dumps({"evidence_count": len(related)}, ensure_ascii=False),
        )
        session.add(case)
        try:
            await session.flush()
            return case
        except IntegrityError:
            await session.rollback()
            continue
    raise RuntimeError("案件编号冲突，5次重试均失败（并发异常）")


async def revoke_violation(
    session: AsyncSession, violation_id: int, reason: str, operator: str
) -> ViolationRecord:
    """误判撤销：标记 revoked，并联动案件 FALSE_POSITIVE → STRIKE_REVOKED → CLOSED。"""
    violation = await session.get(ViolationRecord, violation_id)
    if violation is None:
        raise ValueError(f"违规记录不存在: {violation_id}")
    violation.revoked = True
    violation.revoke_reason = f"{reason}（操作人: {operator}）"

    if violation.case_id:
        case = await session.get(Case, violation.case_id)
        if case and case.status in ("PENDING_REVIEW",):
            validate_transition(case.status, "FALSE_POSITIVE")
            case.status = "FALSE_POSITIVE"
            validate_transition("FALSE_POSITIVE", "STRIKE_REVOKED")
            case.status = "STRIKE_REVOKED"
            validate_transition("STRIKE_REVOKED", "CLOSED")
            case.status = "CLOSED"
            case.closed_at = datetime.now(UTC)
            case.audit_json = json.dumps(
                {**json.loads(case.audit_json), "revoked_by": operator, "revoke_reason": reason},
                ensure_ascii=False,
            )
    await session.commit()
    return violation


async def transition_case(
    session: AsyncSession,
    case_id: int,
    target: str,
    operator: str,
    extra: dict[str, Any] | None = None,
) -> Case:
    """案件状态转换（供 T-301 审批界面调用），校验合法性并记录审计。

    R-102-8：审计记录的 `from` 必须在赋值前捕获（原实现赋值后读取，from=to，错误）。
    """
    case = await session.get(Case, case_id)
    if case is None:
        raise ValueError(f"案件不存在: {case_id}")
    previous = case.status
    validate_transition(previous, target)
    case.status = target
    if target in ("CLOSED",):
        case.closed_at = datetime.now(UTC)
    audit = json.loads(case.audit_json)
    transitions = audit.setdefault("transitions", [])
    transitions.append(
        {
            "from": previous,
            "to": target,
            "operator": operator,
            "extra": extra or {},
            "at": datetime.now(UTC).isoformat(),
        }
    )
    case.audit_json = json.dumps(audit, ensure_ascii=False)
    await session.commit()
    return case
