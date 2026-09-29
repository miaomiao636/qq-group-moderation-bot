# ruff: noqa: E402, I001, F401, F811, ASYNC109, B905
# Reviewer round-5 probe pack (20cccd5), promoted verbatim except the Q01 contract change
# documented in docs/2026-09-18-r132-round5-remediation.md.
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

assert os.environ["APP_ENV"] == "test"
assert os.environ["ACTION_MODE"] == "SHADOW"
assert all(
    os.environ[key] == "false"
    for key in (
        "AI_ENABLED",
        "ONEBOT_ACTIONS_ENABLED",
        "NOTIFICATIONS_ENABLED",
        "NOTIFICATION_QQ_ENABLED",
        "NOTIFICATION_EMAIL_ENABLED",
        "NOTIFICATION_HEARTBEAT_ENABLED",
    )
)
DB_PATH = Path(os.environ["DATABASE_URL"].split(":///", 1)[1]).resolve()
assert DB_PATH.parent.name.startswith("qqbot-test-")
assert DB_PATH.is_relative_to(Path(tempfile.gettempdir()).resolve())

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from app.db import SessionLocal
from app.models import AllowlistMember, AdminChangePlan, AdminAudit
from app.moderation import allowlist
from app.web import agent_confirm
from tests.test_r132_f05_plan_integrity import clients, preview, confirm, seed


async def snapshot(plan_id=None):
    async with SessionLocal() as session:
        rows = (await session.scalars(select(AllowlistMember))).all()
        plan = await session.get(AdminChangePlan, plan_id) if plan_id else None
        audits = (
            await session.scalars(
                select(AdminAudit).where(AdminAudit.action == "allowlist_members_import")
            )
        ).all()
        return {
            "members": {
                (r.provider, r.external_user_id): {
                    "id": r.id,
                    "enabled": r.enabled,
                    "note": r.note,
                    "updated_at": r.updated_at,
                }
                for r in rows
            },
            "plan_status": plan.status if plan else None,
            "import_audits": len(audits),
        }


def test_migrated_schema_rejects_null_updated_at_and_duplicate_identity(clients):
    member_id = asyncio.run(seed(enabled=True))

    async def verify_schema():
        async with SessionLocal() as session:
            columns = (await session.execute(text("PRAGMA table_info(allowlist_members)"))).all()
            assert next(row for row in columns if row[1] == "updated_at")[3] == 1
            ddl = await session.scalar(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='allowlist_members'"
                )
            )
            assert "AUTOINCREMENT" in ddl
            assert "UNIQUE (provider, external_user_id)" in ddl
            with pytest.raises(
                IntegrityError, match="NOT NULL constraint failed: allowlist_members.updated_at"
            ):
                await session.execute(
                    update(AllowlistMember)
                    .where(AllowlistMember.id == member_id)
                    .values(updated_at=None)
                )
                await session.commit()
            await session.rollback()
            session.add(
                AllowlistMember(
                    provider="onebot",
                    external_user_id="920000001",
                    enabled=True,
                    note="duplicate",
                    created_by="synthetic",
                )
            )
            with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                await session.commit()
            await session.rollback()
            rows = (await session.scalars(select(AllowlistMember))).all()
            assert len(rows) == 1 and rows[0].id == member_id and rows[0].updated_at is not None

    asyncio.run(verify_schema())


@pytest.mark.parametrize(
    "other_enabled", [True, False], ids=["external-active", "external-revoked"]
)
def test_to_add_identity_collision_requires_new_preview_without_partial_application(
    clients, monkeypatch, other_enabled
):
    asyncio.run(seed(enabled=True))
    # Two planned additions make partial-application checks meaningful.
    content = "920000001\n920000002\n920000003\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation
    real_apply = allowlist.apply_member_import
    reached = {"claim": 0, "external_insert": 0, "apply": 0}

    async def claim_then_insert(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        reached["claim"] += 1
        async with SessionLocal() as other:
            row, created = await allowlist.add_member(
                other, "920000002", operator="human:synthetic-other", note="external-note"
            )
            assert created
            if not other_enabled:
                await allowlist.set_member_enabled(
                    other, row.id, False, operator="human:synthetic-other"
                )
        reached["external_insert"] += 1
        return claimed

    async def counted_apply(*args, **kwargs):
        reached["apply"] += 1
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_insert)
    monkeypatch.setattr(allowlist, "apply_member_import", counted_apply)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(snapshot(plan_id))
    # Q01（2026-09-19 契约变更，见 docs/2026-09-18-r132-round5-remediation.md）：
    # 同身份并发新增属于"名单已变化" → 引导重新预览（303），不再是 500；计划收敛到
    # 明确终态 FAILED，不留 EXECUTING。下面仍保留"无部分写入、无假审计、外部状态不变、
    # apply 真实到达"的原断言（每个计数都必须为 1，防止提前拒绝造成假绿）。
    assert reached == {"claim": 1, "external_insert": 1, "apply": 1}, (reached, actual)
    assert response.status_code == 303, actual
    assert "/admin/allowlist?notice=" in response.headers.get("location", ""), actual
    assert actual["members"][("onebot", "920000002")]["enabled"] is other_enabled, actual
    assert actual["members"][("onebot", "920000002")]["note"] == "external-note", actual
    assert ("onebot", "920000003") not in actual["members"], actual
    assert actual["import_audits"] == 0 and actual["plan_status"] == "FAILED", actual


@pytest.mark.parametrize("operation", ["revoke-same-user", "insert-planned-user"])
def test_other_provider_same_qq_does_not_poison_plan(clients, monkeypatch, operation):
    asyncio.run(seed(enabled=True))

    async def seed_other():
        async with SessionLocal() as other:
            row, _ = await allowlist.add_member(
                other, "920000001", operator="synthetic", provider="other-provider"
            )
            return row.id

    other_member_id = asyncio.run(seed_other()) if operation == "revoke-same-user" else None
    content = "920000001\n920000002\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation
    reached = []

    async def claim_then_change_other_provider(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        async with SessionLocal() as other:
            if operation == "revoke-same-user":
                await allowlist.set_member_enabled(
                    other, other_member_id, False, operator="human:synthetic-other"
                )
            else:
                await allowlist.add_member(
                    other, "920000002", operator="human:synthetic-other", provider="other-provider"
                )
        reached.append(True)
        return claimed

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_change_other_provider)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(snapshot(plan_id))
    assert reached == [True]
    assert response.status_code == 303 and actual["plan_status"] == "APPLIED", actual
    assert actual["members"][("onebot", "920000001")]["enabled"] is True, actual
    assert actual["members"][("onebot", "920000002")]["enabled"] is True, actual
    if operation == "revoke-same-user":
        assert actual["members"][("other-provider", "920000001")]["enabled"] is False, actual
    else:
        assert actual["members"][("other-provider", "920000002")]["enabled"] is True, actual


@pytest.mark.parametrize("operation", ["disable-enable-aba", "delete-readd-identity"])
def test_unchanged_row_version_catches_aba_and_new_identity(clients, monkeypatch, operation):
    member_id = asyncio.run(seed(enabled=True))
    content = "920000001\n920000002\n"
    plan_id = preview(clients[0], content)
    real_claim = agent_confirm.claim_confirmation
    real_apply = allowlist.apply_member_import
    reached = {"claim": 0, "external_change": 0, "apply": 0}

    async def claim_then_change(*args, **kwargs):
        claimed = await real_claim(*args, **kwargs)
        assert claimed is not None
        reached["claim"] += 1
        async with SessionLocal() as other:
            if operation == "disable-enable-aba":
                await allowlist.set_member_enabled(
                    other, member_id, False, operator="human:synthetic-other"
                )
                await allowlist.set_member_enabled(
                    other, member_id, True, operator="human:synthetic-other"
                )
            else:
                await allowlist.delete_member(other, member_id, operator="human:synthetic-other")
                row, created = await allowlist.add_member(
                    other, "920000001", operator="human:synthetic-other"
                )
                assert created and row.id != member_id
        reached["external_change"] += 1
        return claimed

    async def counted_apply(*args, **kwargs):
        reached["apply"] += 1
        return await real_apply(*args, **kwargs)

    monkeypatch.setattr(agent_confirm, "claim_confirmation", claim_then_change)
    monkeypatch.setattr(allowlist, "apply_member_import", counted_apply)
    response = confirm(clients[0], content, plan_id)
    actual = asyncio.run(snapshot(plan_id))
    assert reached == {"claim": 1, "external_change": 1, "apply": 1}, (reached, actual)
    assert response.status_code == 303 and actual["plan_status"] != "APPLIED", actual
    assert actual["members"][("onebot", "920000001")]["enabled"] is True, actual
    assert ("onebot", "920000002") not in actual["members"] and actual["import_audits"] == 0, actual
