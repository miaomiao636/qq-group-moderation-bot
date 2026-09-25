"""违规记录与人工案件服务（T-104）。

同群+同成员+30天有效违规窗口用于首次待审立案：
- 每次高置信违规只建议撤回并保存证据；
- 30天内两次有效违规新建待审案件；人工结案后再犯新建案件；
- 案件只供人工处理，不按累计次数升级自动处罚；
- 任何情况下**不自动创建踢人任务**；踢人只能来自人工审批（T-301）。

误判撤销：revoke_violation 将记录标记 revoked 并从窗口剔除；案件内全部违规被撤销时，
案件走 FALSE_POSITIVE → STRIKE_REVOKED → CLOSED。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy import case as sql_case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.case_sm import validate_transition
from app.cases.models import Case, ViolationRecord
from app.core.contracts import Provider, StandardMessage
from app.moderation.decision import ModerationDecision

WINDOW_DAYS = 30


async def _generate_case_no(session: AsyncSession) -> str:
    prefix = f"R{datetime.now(UTC):%Y%m%d}"
    numbers = await session.scalars(select(Case.case_no).where(Case.case_no.like(f"{prefix}-%")))
    suffixes = (number.removeprefix(f"{prefix}-") for number in numbers)
    highest = max(
        (int(value) for value in suffixes if value.isascii() and value.isdecimal()), default=0
    )
    return f"{prefix}-{highest + 1:02d}"


class PlannedAction:
    """建议动作（结构中永不存在 kick）。"""

    def __init__(self, action: str, **params: Any) -> None:
        if action != "recall":
            raise ValueError(f"非法自动动作类型: {action}（仅允许撤回）")
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
    """证据快照：中立身份、消息文本、附件元数据、命中规则、决策。"""
    return {
        "message_id": msg.message_id,
        "external_message_id": msg.external_message_id,
        "provider": msg.provider,
        "external_group_id": msg.external_group_id,
        "external_user_id": msg.external_user_id,
        # 旧镜像字段保留，便于混合版本人工比对（contract 阶段移除）
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


def _group_filter(
    model: type[ViolationRecord] | type[Case], provider: Provider, external_group_id: str
) -> Any:
    """按中立群标识过滤；兼容混合版本期间旧代码写入的"仅有旧镜像列"行。"""
    neutral = and_(model.provider == provider, model.external_group_id == external_group_id)
    if provider != "qq_official":
        return neutral
    return or_(
        neutral,
        and_(
            model.provider == "qq_official",
            model.external_group_id == "",
            model.group_openid == external_group_id,
        ),
    )


def _member_filter(
    model: type[ViolationRecord] | type[Case], provider: Provider, external_user_id: str
) -> Any:
    """按中立成员标识过滤；兼容混合版本期间仅有旧镜像列的行。"""
    neutral = and_(model.provider == provider, model.external_user_id == external_user_id)
    if provider != "qq_official":
        return neutral
    return or_(
        neutral,
        and_(
            model.provider == "qq_official",
            model.external_user_id == "",
            model.member_openid == external_user_id,
        ),
    )


async def count_active_violations(
    session: AsyncSession,
    external_group_id: str,
    external_user_id: str,
    *,
    provider: Provider = "qq_official",
) -> int:
    """30 天窗口内的有效违规数（revoked 不计）。

    T-305：以中立身份列为准，兼 容仅有旧镜像列的混合版本数据。
    """
    window_start = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
    stmt = (
        select(func.count())
        .select_from(ViolationRecord)
        .where(
            _group_filter(ViolationRecord, provider, external_group_id),
            _member_filter(ViolationRecord, provider, external_user_id),
            ViolationRecord.revoked.is_(False),
            ViolationRecord.created_at >= window_start,
        )
    )
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def _case_for_violation(
    session: AsyncSession, msg: StandardMessage, violation: ViolationRecord
) -> Case | None:
    identity = (
        _group_filter(Case, msg.provider, msg.external_group_id),
        _member_filter(Case, msg.provider, msg.external_user_id),
    )
    pending = (
        await session.scalars(
            select(Case)
            .where(*identity, Case.status == "PENDING_REVIEW", Case.archived.is_(False))
            .order_by(Case.id.desc())
            .limit(1)
        )
    ).first()
    if pending is not None:
        ids = json.loads(pending.violation_ids_json)
        if not isinstance(ids, list) or any(type(value) is not int for value in ids):
            raise ValueError("待审案件证据索引异常")
        if violation.id not in ids:
            pending.violation_ids_json = json.dumps([*ids, violation.id])
        violation.case_id = pending.id
        return pending

    last_closed = (
        await session.scalars(
            select(Case)
            .where(*identity, Case.status == "CLOSED")
            .order_by(Case.closed_at.desc(), Case.id.desc())
            .limit(1)
        )
    ).first()
    # Read only the most recent confirmed disposition. JSON guards prevent a
    # damaged legacy audit from aborting intake; cleanup retains transitions.
    safe_audit = sql_case((func.json_valid(Case.audit_json) == 1, Case.audit_json), else_="{}")
    transitions = sql_case(
        (
            func.json_type(safe_audit, "$.transitions") == "array",
            func.json_extract(safe_audit, "$.transitions"),
        ),
        else_="[]",
    )
    item = func.json_each(transitions).table_valued("value").alias("transition_item")
    safe_item = sql_case((func.json_valid(item.c.value) == 1, item.c.value), else_="{}")
    confirmed = exists(
        select(1)
        .select_from(item)
        .where(func.json_extract(safe_item, "$.to").in_(("KEEP", "KICKED")))
    )
    prior_confirmed = (
        await session.scalars(
            select(Case)
            .where(*identity, Case.status == "CLOSED", confirmed)
            .order_by(Case.closed_at.desc(), Case.id.desc())
            .limit(1)
        )
    ).first()
    if prior_confirmed is not None:
        # A repeat case starts with the new event, even if the prior case has
        # been archived; the old evidence remains attached to that prior case.
        related = [violation]
    else:
        window_start = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
        if last_closed is not None and last_closed.closed_at is not None:
            window_start = max(window_start, last_closed.closed_at.replace(tzinfo=UTC))
        related = list(
            await session.scalars(
                select(ViolationRecord)
                .where(
                    _group_filter(ViolationRecord, msg.provider, msg.external_group_id),
                    _member_filter(ViolationRecord, msg.provider, msg.external_user_id),
                    ViolationRecord.revoked.is_(False),
                    ViolationRecord.case_id.is_(None),
                    ViolationRecord.created_at >= window_start,
                )
                .order_by(ViolationRecord.id)
            )
        )
        if len(related) < 2:
            return None
    for _attempt in range(5):
        case = Case(
            case_no=await _generate_case_no(session),
            group_openid=msg.external_group_id,
            member_openid=msg.external_user_id,
            provider=msg.provider,
            external_group_id=msg.external_group_id,
            external_user_id=msg.external_user_id,
            status="PENDING_REVIEW",
            violation_ids_json=json.dumps([record.id for record in related]),
            audit_json=json.dumps(
                {
                    "evidence_count": len(related),
                    **({"prior_case_id": prior_confirmed.id} if prior_confirmed else {}),
                },
                ensure_ascii=False,
            ),
        )
        try:
            async with session.begin_nested():
                session.add(case)
                await session.flush()
        except IntegrityError:
            continue
        for record in related:
            record.case_id = case.id
        return case
    raise RuntimeError("案件编号冲突，5次重试均失败")


async def record_violation(
    session: AsyncSession,
    msg: StandardMessage,
    decision: ModerationDecision,
) -> ViolationOutcome:
    """记录高置信违规；自动动作仅撤回，待审案件不执行处罚。

    调用方负责：执行 planned_actions 中的动作并回写 action_result_json。
    SQLite 的计数、违规写入和立案共享写事务；不要在取得写锁前计数。
    """
    if decision.verdict != "violation_high":
        raise ValueError("只有 violation_high 决策才能记录有效违规")
    if decision.is_protected_sender or msg.sender.role in ("owner", "admin"):
        raise ValueError("保护角色（群主/管理员）不得进入自动撤回流程")

    if session.get_bind().dialect.name != "sqlite":
        raise RuntimeError("违规记录事务目前仅支持 SQLite")
    # A zero-row UPDATE acquires SQLite's database-wide writer reservation without
    # modifying evidence. It also works inside the caller's existing transaction,
    # unlike issuing another BEGIN IMMEDIATE or committing the caller's work.
    # Keep the lock through count + insert until the commit below.
    await session.execute(text("UPDATE violation_records SET revoked = revoked WHERE 0"))

    existing = await count_active_violations(
        session,
        msg.external_group_id,
        msg.external_user_id,
        provider=msg.provider,
    )
    strike_no = existing + 1

    violation = ViolationRecord(
        group_openid=msg.external_group_id,
        member_openid=msg.external_user_id,
        provider=msg.provider,
        external_group_id=msg.external_group_id,
        external_user_id=msg.external_user_id,
        message_id=msg.message_id,
        category=decision.category or "other",
        confidence=decision.confidence,
        rule_hits_json=json.dumps([h.model_dump() for h in decision.rule_hits], ensure_ascii=False),
        message_snapshot_json=json.dumps(_snapshot(msg, decision), ensure_ascii=False),
    )
    session.add(violation)
    await session.flush()  # 取得 violation.id

    case = await _case_for_violation(session, msg, violation)
    planned: list[PlannedAction] = [
        PlannedAction("recall", external_message_id=msg.external_message_id)
    ]
    await session.commit()
    return ViolationOutcome(
        strike_no=strike_no, violation=violation, planned_actions=planned, case=case
    )


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
            ids = json.loads(case.violation_ids_json)
            remaining = await session.scalar(
                select(ViolationRecord.id)
                .where(
                    or_(ViolationRecord.id.in_(ids), ViolationRecord.case_id == case.id),
                    ViolationRecord.revoked.is_(False),
                )
                .limit(1)
            )
            if remaining is None:
                validate_transition(case.status, "FALSE_POSITIVE")
                case.status = "FALSE_POSITIVE"
                validate_transition("FALSE_POSITIVE", "STRIKE_REVOKED")
                case.status = "STRIKE_REVOKED"
                validate_transition("STRIKE_REVOKED", "CLOSED")
                case.status = "CLOSED"
                case.closed_at = datetime.now(UTC)
                case.audit_json = json.dumps(
                    {
                        **json.loads(case.audit_json),
                        "revoked_by": operator,
                        "revoke_reason": reason,
                    },
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
    *,
    commit: bool = True,
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
    if commit:
        await session.commit()
    else:
        await session.flush()
    return case
