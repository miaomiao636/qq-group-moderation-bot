"""Bounded case summaries and atomic KEEP/CLOSED plans; never member actions.

Uses the existing change-plan table. Callers own the transaction and must reserve
the SQLite writer before creating/confirming a plan. Exports use a read snapshot.
"""

from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.models import Case, ViolationRecord
from app.cases.service import transition_case
from app.models import AdminAudit, AdminChangePlan, GroupAlias, ProviderGroupSettings

CLOSE_LIMIT = 200
EXPORT_LIMIT = 5000
EVIDENCE_LIMIT = 20000
ACTION = "case_batch_keep"


def selected_ids(case_ids: list[int] | None, compact: str) -> list[int]:
    """Decode the single-field browser payload while accepting older page forms."""
    if not compact:
        return case_ids or []
    if case_ids or len(compact) > 120_000:
        raise HTTPException(422, "案件编号无效或数量过多")
    try:
        ids = json.loads(compact)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "案件编号格式无效") from exc
    if not isinstance(ids, list) or len(ids) > EXPORT_LIMIT:
        raise HTTPException(422, "案件编号无效或数量过多")
    parsed: list[int] = []
    for raw in ids:
        if type(raw) is int:
            value = raw
        elif (
            type(raw) is str
            and 1 <= len(raw) <= 19
            and raw[0] != "0"
            and all("0" <= digit <= "9" for digit in raw)
        ):
            value = int(raw)
        else:
            raise HTTPException(422, "案件编号无效或数量过多")
        if value <= 0 or value > 2**63 - 1:
            raise HTTPException(422, "案件编号无效或数量过多")
        parsed.append(value)
    return parsed


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def identity(row: Case | ViolationRecord) -> tuple[str, str, str]:
    legacy = row.provider == "qq_official"
    return (
        row.provider,
        row.external_group_id or (row.group_openid if legacy else ""),
        row.external_user_id or (row.member_openid if legacy else ""),
    )


async def names(session: AsyncSession) -> dict[tuple[str, str], str]:
    result = {
        ("qq_official", r.group_openid): r.name for r in await session.scalars(select(GroupAlias))
    }
    result.update(
        {
            (r.provider, r.external_group_id): r.name
            for r in await session.scalars(select(ProviderGroupSettings))
            if r.name.strip()
        }
    )
    return result


def conditions(filters: dict[str, str], group_names: dict[tuple[str, str], str]) -> list[Any]:
    try:
        start = (
            datetime.strptime(filters["date_from"], "%Y-%m-%d")
            if filters.get("date_from")
            else None
        )
        end = datetime.strptime(filters["date_to"], "%Y-%m-%d") if filters.get("date_to") else None
        if start and end and start > end:
            raise ValueError("reversed")
        exclusive_end = end + timedelta(days=1) if end else None
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "日期范围无效，请使用有效的起止日期") from exc
    if filters.get("archived", "") not in {"", "0", "1"}:
        raise HTTPException(422, "归档筛选无效")
    result: list[Any] = [Case.archived.is_(filters.get("archived") == "1")]
    if filters.get("status"):
        result.append(Case.status == filters["status"])
    needle = filters.get("group", "").strip()
    if needle:
        group_id = case(
            (and_(Case.provider == "qq_official", Case.external_group_id == ""), Case.group_openid),
            else_=Case.external_group_id,
        )
        matches = [
            and_(Case.provider == provider, group_id == gid)
            for (provider, gid), name in group_names.items()
            if needle in name
        ]
        # Historical incomplete rows must remain discoverable by their old
        # group marker. This is a read-only search fallback, NOT an identity
        # mapping: identity() still refuses the OneBot mirror for export/close.
        legacy_groups = {needle} | {
            gid
            for (provider, gid), name in group_names.items()
            if provider == "qq_official" and needle in name
        }
        result.append(
            or_(
                group_id == needle,
                *matches,
                and_(Case.external_group_id == "", Case.group_openid.in_(legacy_groups)),
            )
        )
    if start:
        result.append(Case.created_at >= start)
    if exclusive_end:
        result.append(Case.created_at < exclusive_end)
    return result


async def select_cases(
    session: AsyncSession, scope: str, ids: list[int], filters: dict[str, str], *, limit: int
) -> list[Case]:
    if scope not in {"selected", "filtered"}:
        raise HTTPException(422, "请选择勾选案件或当前筛选全部")
    if len(ids) > EXPORT_LIMIT or any(value <= 0 or value > 2**63 - 1 for value in ids):
        raise HTTPException(422, "案件编号无效或数量过多")
    ids = sorted(set(ids))
    if scope == "selected":
        if not ids or len(ids) > limit:
            raise HTTPException(422, f"请选择 1 至 {limit} 个案件")
        query = select(Case).where(Case.id.in_(ids))
    else:
        query = select(Case).where(*conditions(filters, await names(session)))
    rows = list(
        await session.scalars(
            query.order_by(Case.created_at.desc(), Case.id.desc()).limit(limit + 1)
        )
    )
    if scope == "selected" and len(rows) != len(ids):
        raise HTTPException(409, "所选案件已变化，请刷新列表")
    if not rows:
        raise HTTPException(422, "没有符合条件的案件")
    if len(rows) > limit:
        raise HTTPException(422, f"本次最多 {limit} 个案件，请缩小筛选范围；未执行部分处理")
    return rows


async def summaries(session: AsyncSession, rows: list[Case]) -> list[dict[str, Any]]:
    explicit: dict[int, set[int]] = {}
    for row in rows:
        try:
            values = json.loads(row.violation_ids_json)
            if not isinstance(values, list) or any(type(v) is not int or v <= 0 for v in values):
                raise ValueError("invalid evidence ids")
            explicit[row.id] = set(values)
        except (TypeError, ValueError) as exc:
            raise HTTPException(409, f"案件 {row.case_no} 的证据索引异常，请逐案检查") from exc
    all_ids = set().union(*explicit.values())
    if len(all_ids) > EVIDENCE_LIMIT:
        raise HTTPException(422, "证据数量过多，请缩小筛选范围")
    records = list(
        await session.scalars(
            select(ViolationRecord)
            .where(
                or_(
                    ViolationRecord.case_id.in_([r.id for r in rows]),
                    ViolationRecord.id.in_(all_ids),
                )
            )
            .order_by(ViolationRecord.id)
            .limit(EVIDENCE_LIMIT + 1)
        )
    )
    if len(records) > EVIDENCE_LIMIT:
        raise HTTPException(422, "证据数量过多，请缩小筛选范围")
    by_id = {r.id: r for r in records}
    reverse: dict[int, set[int]] = {}
    for record in records:
        if record.case_id is not None:
            reverse.setdefault(record.case_id, set()).add(record.id)
    group_names = await names(session)
    result = []
    for row in rows:
        evidence_ids = explicit[row.id] | reverse.get(row.id, set())
        evidence = [by_id[i] for i in sorted(evidence_ids) if i in by_id]
        provider, gid, uid = identity(row)
        if any(identity(record) != (provider, gid, uid) for record in evidence):
            raise HTTPException(409, f"案件 {row.case_no} 的关联证据身份不一致，请逐案检查")
        name = group_names.get((provider, gid), "")
        # Include reverse-linked evidence: a new violation can reuse the case
        # without changing any Case columns. Store hashes, not raw evidence copies.
        snapshot = {
            "case": {c.name: getattr(row, c.name) for c in Case.__table__.columns},
            "group_name": name,
            "evidence": [
                {c.name: getattr(r, c.name) for c in ViolationRecord.__table__.columns}
                for r in evidence
            ],
        }
        result.append(
            {
                "id": row.id,
                "case_no": row.case_no,
                "provider": provider,
                "group": gid,
                "member": uid,
                "name": name,
                "status": row.status,
                "created_at": str(row.created_at),
                "closed_at": str(row.closed_at or ""),
                "count": len(evidence),
                "basis": "; ".join(sorted({r.category for r in evidence if not r.revoked})),
                "evidence_ids": ";".join(str(i) for i in sorted(evidence_ids)),
                "missing": len(evidence_ids - by_id.keys()),
                "fingerprint": sha256(canonical(snapshot).encode()).hexdigest(),
            }
        )
    return result


def csv_cell(value: object) -> str:
    value = str(value)
    stripped = value.lstrip()
    # Quoting alone does not stop spreadsheet formulas. Preserve long numeric
    # identifiers as text, without embedding executable Excel formulas.
    if (
        (stripped and stripped[0] in "=+-@")
        or value.startswith(("\t", "\r", "\n"))
        or (value.isdecimal() and (len(value) > 15 or value.startswith("0")))
    ):
        return "'" + value
    return value


def export_csv(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    writer.writerow(
        [
            "案件编号",
            "来源",
            "群名",
            "群号",
            "QQ号",
            "群身份",
            "成员身份",
            "状态",
            "创建时间(UTC)",
            "结案时间(UTC)",
            "违规记录数",
            "案件依据",
            "证据编号",
            "缺失证据数",
        ]
    )
    for r in rows:
        numeric = r["provider"] == "onebot"
        writer.writerow(
            [
                csv_cell(v)
                for v in [
                    r["case_no"],
                    r["provider"],
                    r["name"],
                    r["group"]
                    if numeric and r["group"].isascii() and r["group"].isdecimal()
                    else "",
                    r["member"]
                    if numeric and r["member"].isascii() and r["member"].isdecimal()
                    else "",
                    r["group"],
                    r["member"],
                    r["status"],
                    r["created_at"],
                    r["closed_at"],
                    r["count"],
                    r["basis"],
                    r["evidence_ids"],
                    r["missing"],
                ]
            ]
        )
    return output.getvalue().encode("utf-8-sig")


def eligible(rows: list[Case]) -> None:
    invalid = [r.case_no for r in rows if r.status != "PENDING_REVIEW" or r.archived]
    if invalid:
        raise HTTPException(
            409,
            "仅可批量处理未归档的待审案件；请筛选待处理后重新预览。不可处理："
            + "、".join(invalid[:10]),
        )
    if any(not all(identity(row)) for row in rows):
        raise HTTPException(409, "案件缺少权威群或成员身份，请逐案检查后再结案")


def partition_for_close(rows: list[Case]) -> tuple[list[Case], list[Case]]:
    """Skip already closed selections only at preview; never skip changed plans."""
    pending = [row for row in rows if row.status == "PENDING_REVIEW" and not row.archived]
    closed = [row for row in rows if row.status == "CLOSED"]
    if len(pending) + len(closed) != len(rows):
        invalid = [
            row.case_no
            for row in rows
            if row.status != "CLOSED" and (row.status != "PENDING_REVIEW" or row.archived)
        ]
        raise HTTPException(
            409, "包含仍在审批中或归档异常的案件，请逐案核对：" + "、".join(invalid[:10])
        )
    if len(pending) > CLOSE_LIMIT:
        raise HTTPException(422, f"本次最多处理 {CLOSE_LIMIT} 个待审案件，请缩小范围")
    eligible(pending)
    return pending, closed


async def create_plan(
    session: AsyncSession, rows: list[Case], actor: str, reason: str
) -> tuple[AdminChangePlan, list[dict[str, Any]]]:
    reason = reason.strip() or "人工处理"
    if not 1 <= len(reason) <= 500:
        raise HTTPException(422, "请填写 1 至 500 字的结案原因")
    eligible(rows)
    preview = await summaries(session, rows)
    plan = AdminChangePlan(
        id=secrets.token_urlsafe(32),
        action=ACTION,
        requestor=actor,
        params_json=canonical({"ids": [r.id for r in rows], "reason": reason}),
        expected_state_json=canonical({str(r["id"]): r["fingerprint"] for r in preview}),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session.add(plan)
    session.add(
        AdminAudit(
            operator=actor,
            action="case_batch_preview",
            target_type="admin_plan",
            target_id=plan.id,
            detail_json=canonical({"count": len(rows)}),
        )
    )
    await session.flush()
    return plan, preview


async def confirm_plan(session: AsyncSession, plan_id: str, actor: str) -> int:
    # Reserve the writer BEFORE all reads, including plan consumption, case
    # fingerprints and the transition chain; failures roll back the whole batch.
    await session.execute(
        update(AdminChangePlan)
        .where(AdminChangePlan.id == plan_id)
        .values(status=AdminChangePlan.status)
    )
    plan = await session.get(AdminChangePlan, plan_id)
    if plan is None or plan.action != ACTION or plan.requestor != actor:
        raise HTTPException(409, "预览不存在或不属于当前登录，请重新预览")
    params = json.loads(plan.params_json)
    if plan.status == "EXECUTED":
        return len(params["ids"])
    if plan.status != "PENDING" or plan.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
        raise HTTPException(409, "预览已过期或已失效，请重新预览")
    rows = await select_cases(session, "selected", params["ids"], {}, limit=CLOSE_LIMIT)
    eligible(rows)
    current = await summaries(session, rows)
    if {str(r["id"]): r["fingerprint"] for r in current} != json.loads(plan.expected_state_json):
        raise HTTPException(409, "案件或关联证据已变化，本批未结案，请重新预览")
    for row in rows:
        extra = {"batch_plan": plan.id, "reason": params["reason"]}
        await transition_case(session, row.id, "KEEP", actor, extra, commit=False)
        await transition_case(session, row.id, "CLOSED", actor, extra, commit=False)
        session.add(
            AdminAudit(
                operator=actor,
                action=ACTION,
                target_type="case",
                target_id=str(row.id),
                detail_json=canonical(extra),
            )
        )
    plan.status = "EXECUTED"
    plan.approved_by = actor
    session.add(
        AdminAudit(
            operator=actor,
            action="case_batch_complete",
            target_type="admin_plan",
            target_id=plan.id,
            detail_json=canonical({"count": len(rows), "reason": params["reason"]}),
        )
    )
    await session.flush()
    return len(rows)
