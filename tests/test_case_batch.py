"""Synthetic, isolated admin batch operations; no live messages or member actions."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from app.cases.models import Case, ViolationRecord
from app.models import AdminAudit, AdminChangePlan, ProviderGroupSettings
from app.web import auth, case_batch
from sqlalchemy import event, select

from tests import test_longterm_web

web_ui = test_longterm_web.web_ui


async def seed(factory, count=2):
    async with factory() as session:
        session.add(
            ProviderGroupSettings(provider="onebot", external_group_id="123456", name="合成群")
        )
        for index in range(count):
            case = Case(
                case_no=f"BATCH-{index}",
                provider="onebot",
                group_openid="old-group",
                member_openid="old-user",
                external_group_id="123456",
                external_user_id=str(200000 + index),
            )
            session.add(case)
            await session.flush()
            session.add(
                ViolationRecord(
                    provider="onebot",
                    group_openid="old-group",
                    member_openid="old-user",
                    external_group_id="123456",
                    external_user_id=case.external_user_id,
                    message_id=f"msg-{index}",
                    category="ad",
                    confidence=0.95,
                    case_id=case.id,
                )
            )
        await session.commit()


async def preview(client, csrf, **kw):
    response = await client.post(
        "/admin/cases/batch-preview",
        data={
            "csrf": csrf,
            "scope": "selected",
            "case_ids": ["1", "2"],
            "reason": "已人工核对，本次保留成员",
            **kw,
        },
    )
    assert response.status_code == 200, response.text
    return re.search(r'name="plan_id" value="([^"]+)"', response.text)[1]


async def statuses(factory):
    async with factory() as session:
        return list(await session.scalars(select(Case.status).order_by(Case.id)))


async def test_export_authoritative_ids_and_summary_only(web_ui):
    client, factory, _, csrf = web_ui
    await seed(factory)
    response = await client.post(
        "/admin/cases/batch-export", data={"csrf": csrf, "scope": "selected", "case_ids": ["2"]}
    )
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["cache-control"] == "no-store"
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["群名"] == "合成群"
    assert rows[0]["群号"] == "123456" and rows[0]["QQ号"] == "200001"
    assert rows[0]["违规记录数"] == "1"
    assert "广告" in rows[0]["案件依据"] or "ad" in rows[0]["案件依据"]
    assert "old-user" not in response.text and "message_snapshot_json" not in response.text


async def test_preview_and_idempotent_atomic_close_preserve_violations(web_ui):
    client, factory, _, csrf = web_ui
    await seed(factory)
    plan_id = await preview(client, csrf)
    assert await statuses(factory) == ["PENDING_REVIEW"] * 2
    for _ in range(2):
        response = await client.post(
            "/admin/cases/batch-confirm", data={"csrf": csrf, "plan_id": plan_id}
        )
        assert response.status_code == 303, response.text
    assert await statuses(factory) == ["CLOSED"] * 2
    async with factory() as session:
        records = list(await session.scalars(select(ViolationRecord)))
        assert len(records) == 2 and not any(r.revoked for r in records)
        cases = list(await session.scalars(select(Case)))
        for case in cases:
            transitions = json.loads(case.audit_json)["transitions"]
            assert [t["to"] for t in transitions] == ["KEEP", "CLOSED"]
            assert "已人工核对" in case.audit_json
        audits = list(
            await session.scalars(select(AdminAudit).where(AdminAudit.action == "case_batch_keep"))
        )
        assert len(audits) == 2


@pytest.mark.parametrize("drift", ["status", "new_evidence", "revoked", "identity"])
async def test_drift_invalidates_entire_preview(web_ui, drift):
    client, factory, _, csrf = web_ui
    await seed(factory)
    plan_id = await preview(client, csrf)
    async with factory() as session:
        case = await session.get(Case, 2)
        if drift == "status":
            case.status = "MANUAL_PENDING"
        elif drift == "identity":
            case.external_user_id = "999999"
        elif drift == "revoked":
            (await session.get(ViolationRecord, 2)).revoked = True
        else:
            session.add(
                ViolationRecord(
                    group_openid="old-group",
                    member_openid="old-user",
                    message_id="new",
                    category="ad",
                    confidence=0.95,
                    case_id=2,
                )
            )
        await session.commit()
    before = await statuses(factory)
    response = await client.post(
        "/admin/cases/batch-confirm", data={"csrf": csrf, "plan_id": plan_id}
    )
    assert response.status_code == 409
    assert await statuses(factory) == before


async def test_audit_failure_rolls_back_then_retry(web_ui):
    client, factory, engine, csrf = web_ui
    await seed(factory)
    plan_id = await preview(client, csrf)

    def fail(_conn, _cursor, statement, _params, _context, _many):
        if statement.lower().startswith("insert into admin_audits"):
            raise RuntimeError("synthetic audit failure")

    event.listen(engine.sync_engine, "before_cursor_execute", fail)
    try:
        response = await client.post(
            "/admin/cases/batch-confirm", data={"csrf": csrf, "plan_id": plan_id}
        )
        assert response.status_code == 500
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail)
    assert await statuses(factory) == ["PENDING_REVIEW"] * 2
    assert (
        await client.post("/admin/cases/batch-confirm", data={"csrf": csrf, "plan_id": plan_id})
    ).status_code == 303


@pytest.mark.parametrize("path", ["batch-preview", "batch-confirm", "batch-export"])
async def test_batch_requires_cookie_and_csrf(web_ui, path):
    client, _, _, csrf = web_ui
    response = await client.post(f"/admin/cases/{path}", data={"csrf": "bad"})
    assert response.status_code == 403
    client.cookies.clear()
    response = await client.post(
        f"/admin/cases/{path}",
        data={"csrf": csrf},
        headers={"Authorization": "Bearer test-agent-token"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("mutation", ["expired", "other_session", "other_action", "approved"])
async def test_invalid_plan_cannot_close(web_ui, mutation):
    client, factory, _, csrf = web_ui
    await seed(factory)
    plan_id = await preview(client, csrf)
    extra_token = None
    if mutation == "other_session":
        extra_token = auth.login("admin", "test-admin-pass")
        client.cookies.clear()
        client.cookies.set(auth.SESSION_COOKIE, extra_token)
        csrf = auth.csrf_token(extra_token)
    else:
        async with factory() as session:
            plan = await session.get(AdminChangePlan, plan_id)
            if mutation == "expired":
                plan.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif mutation == "other_action":
                plan.action = "group_settings"
            else:
                plan.status = "APPROVED"
            await session.commit()
    try:
        response = await client.post(
            "/admin/cases/batch-confirm", data={"csrf": csrf, "plan_id": plan_id}
        )
        assert response.status_code == 409
        assert await statuses(factory) == ["PENDING_REVIEW"] * 2
    finally:
        if extra_token:
            auth.logout(extra_token)


async def test_concurrent_confirm_consumes_once(web_ui):
    client, factory, _, csrf = web_ui
    await seed(factory)
    plan_id = await preview(client, csrf)
    responses = await asyncio.gather(
        *(
            client.post("/admin/cases/batch-confirm", data={"csrf": csrf, "plan_id": plan_id})
            for _ in range(2)
        )
    )
    assert [r.status_code for r in responses] == [303, 303]
    async with factory() as session:
        assert (
            len(
                list(
                    await session.scalars(
                        select(AdminAudit).where(AdminAudit.action == "case_batch_keep")
                    )
                )
            )
            == 2
        )
        assert (
            len(
                list(
                    await session.scalars(
                        select(AdminAudit).where(AdminAudit.action == "case_batch_complete")
                    )
                )
            )
            == 1
        )


async def test_filtered_export_spans_pages_but_confirmation_never_adds_new_cases(web_ui):
    client, factory, _, csrf = web_ui
    await seed(factory, 51)
    response = await client.get("/admin/?status=PENDING_REVIEW&group=合成群")
    assert response.status_code == 200
    assert len(re.findall(r"name=case_ids value=", response.text)) == 50
    assert "BATCH-50" in response.text
    assert 'id="case-batch"' in response.text and "导出 CSV" in response.text
    batch_html = response.text.split('id="case-batch"', 1)[1]
    assert "old-group" not in batch_html and "old-user" not in batch_html
    assert "123456" in batch_html and "200050" in batch_html and "onebot" in batch_html
    response = await client.post(
        "/admin/cases/batch-export", data={"csrf": csrf, "scope": "filtered", "group": "合成群"}
    )
    assert response.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))) == 51
    plan_id = await preview(client, csrf, scope="filtered", group="合成群", status="PENDING_REVIEW")
    async with factory() as session:
        session.add(
            Case(
                case_no="NEW",
                group_openid="x",
                member_openid="x",
                provider="onebot",
                external_group_id="123456",
                external_user_id="299999",
            )
        )
        await session.commit()
    response = await client.post(
        "/admin/cases/batch-confirm", data={"csrf": csrf, "plan_id": plan_id, "case_ids": "52"}
    )
    assert response.status_code == 303
    assert await statuses(factory) == ["CLOSED"] * 51 + ["PENDING_REVIEW"]


async def test_export_provider_isolation_formula_and_openid(web_ui):
    client, factory, _, csrf = web_ui
    await seed(factory)
    async with factory() as session:
        session.add(
            ProviderGroupSettings(
                provider="qq_official", external_group_id="123456", name="  =SUM(1,2)\n备注"
            )
        )
        session.add(
            Case(
                case_no="OFFICIAL",
                provider="qq_official",
                group_openid="old",
                member_openid="old",
                external_group_id="123456",
                external_user_id="openid-123",
            )
        )
        (
            await session.get(ProviderGroupSettings, ("onebot", "123456"))
        ).name = '<script>alert("bad")</script>'
        await session.commit()
    response = await client.post(
        "/admin/cases/batch-export", data={"csrf": csrf, "scope": "filtered", "group": "SUM"}
    )
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["案件编号"] == "OFFICIAL"
    assert rows[0]["QQ号"] == "" and rows[0]["群号"] == ""
    assert rows[0]["成员身份"] == "openid-123"
    assert rows[0]["群名"].startswith("'  =")
    response = await client.post(
        "/admin/cases/batch-preview",
        data={"csrf": csrf, "case_ids": ["1"], "reason": "<script>secret()</script>"},
    )
    assert response.status_code == 200
    assert "<script>alert" not in response.text and "<script>secret" not in response.text
    assert "&lt;script&gt;" in response.text


@pytest.mark.parametrize(
    "value",
    [
        "=CMD()",
        "  +CMD()",
        "\tname",
        "\nname",
        "@test",
        "-20",
        "  \r=evil",
        "00123",
        "1234567890123456",
    ],
)
def test_csv_formula_and_numeric_text(value):
    assert case_batch.csv_cell(value).startswith("'")


@pytest.mark.parametrize(
    "data,code",
    [
        ({"case_ids": []}, 422),
        ({"case_ids": ["999"]}, 409),
        ({"case_ids": ["-1"]}, 422),
        ({"reason": " "}, 422),
        ({"scope": "all"}, 422),
        ({"scope": "filtered", "date_from": "2026-99-00"}, 422),
        ({"scope": "filtered", "date_from": "2026-01-02", "date_to": "2026-01-01"}, 422),
        ({"scope": "filtered", "date_to": "9999-12-31"}, 422),
    ],
)
async def test_bad_selection_is_rejected_without_writes(web_ui, data, code):
    client, factory, _, csrf = web_ui
    await seed(factory)
    response = await client.post(
        "/admin/cases/batch-preview",
        data={"csrf": csrf, "scope": "selected", "case_ids": ["1"], "reason": "reviewed", **data},
    )
    assert response.status_code == code
    assert await statuses(factory) == ["PENDING_REVIEW"] * 2


async def test_limits_and_ineligible_not_silently_skipped(web_ui, monkeypatch):
    client, factory, _, csrf = web_ui
    await seed(factory, 3)
    monkeypatch.setattr(case_batch, "CLOSE_LIMIT", 2)
    monkeypatch.setattr(case_batch, "EXPORT_LIMIT", 2)
    for path in ("batch-export", "batch-preview"):
        response = await client.post(
            f"/admin/cases/{path}", data={"csrf": csrf, "scope": "filtered", "reason": "reviewed"}
        )
        assert response.status_code == 422
    async with factory() as session:
        (await session.get(Case, 2)).archived = True
        await session.commit()
    response = await client.post(
        "/admin/cases/batch-preview",
        data={"csrf": csrf, "case_ids": ["1", "2"], "reason": "reviewed"},
    )
    assert response.status_code == 409
    assert await statuses(factory) == ["PENDING_REVIEW"] * 3


async def test_duplicate_ids_and_disabled_delete(web_ui):
    client, factory, _, csrf = web_ui
    await seed(factory)
    plan_id = await preview(client, csrf, case_ids=["1", "1"])
    assert (
        await client.post("/admin/cases/batch-confirm", data={"csrf": csrf, "plan_id": plan_id})
    ).status_code == 303
    response = await client.post(
        "/admin/cases/batch-delete", data={"csrf": csrf, "case_ids": ["1", "2"]}
    )
    assert "停用" in response.text
    assert await statuses(factory) == ["CLOSED", "PENDING_REVIEW"]


@pytest.mark.parametrize("link", ["explicit", "reverse"])
async def test_mismatched_evidence_identity_rejected(web_ui, link):
    client, factory, _, csrf = web_ui
    await seed(factory)
    async with factory() as session:
        record = await session.get(ViolationRecord, 1)
        record.external_user_id = "other-member"
        if link == "explicit":
            record.case_id = None
            (await session.get(Case, 1)).violation_ids_json = "[1]"
        await session.commit()
    for path in ("batch-export", "batch-preview"):
        response = await client.post(
            f"/admin/cases/{path}", data={"csrf": csrf, "case_ids": ["1"], "reason": "reviewed"}
        )
        assert response.status_code == 409
        assert "身份不一致" in response.text
    assert await statuses(factory) == ["PENDING_REVIEW"] * 2


async def test_legacy_official_only_identity_fallback(web_ui):
    client, factory, _, csrf = web_ui
    await seed(factory)
    async with factory() as session:
        session.add(
            Case(case_no="LEGACY", group_openid="official-group", member_openid="official-member")
        )
        session.add(
            Case(
                case_no="BROKEN-ONEBOT",
                provider="onebot",
                group_openid="333333",
                member_openid="444444",
            )
        )
        await session.commit()
    response = await client.post(
        "/admin/cases/batch-export", data={"csrf": csrf, "case_ids": ["3", "4"]}
    )
    rows = {
        r["案件编号"]: r for r in csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    }
    assert rows["LEGACY"]["成员身份"] == "official-member" and rows["LEGACY"]["QQ号"] == ""
    assert rows["BROKEN-ONEBOT"]["成员身份"] == "" and rows["BROKEN-ONEBOT"]["QQ号"] == ""
