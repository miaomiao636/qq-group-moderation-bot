"""R-108：旧删除入口停用后不得破坏共享证据、纠错映射或动作归属。

使用 conftest 的迁移后临时数据库及合成数据；TestClient 不进入 lifespan，
不启动 OneBot/通知 worker，也不调用真实模型或 QQ 接口。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Generator
from dataclasses import dataclass

import pytest
from app.cases.models import Case, ViolationRecord
from app.cases.service import count_active_violations
from app.core.routing import GroupProviderRoute, resolve_action_provider
from app.db import SessionLocal
from app.models import GroupActionOwner, GroupAlias, HiddenGroup, ProviderGroupSettings
from app.moderation.feedback import record_feedback
from app.runtime.models import ShadowDecision
from app.web import auth
from fastapi.testclient import TestClient
from sqlalchemy import select


@dataclass(frozen=True)
class BusinessData:
    group: str
    member: str
    closed_case_id: int
    pending_case_id: int
    internal_message_id: str
    violation_ids: tuple[int, int]


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from app.main import app

    http = TestClient(app)
    try:
        yield http
    finally:
        token = http.cookies.get(auth.SESSION_COOKIE)
        if token:
            auth.logout(token)
        http.close()


@pytest.fixture()
def business_data() -> BusinessData:
    async def seed() -> BusinessData:
        suffix = uuid.uuid4().hex
        group, member = f"synthetic-group-{suffix}", f"synthetic-member-{suffix}"
        external_message_id = f"synthetic-message-{suffix}"
        internal_message_id = f"onebot:synthetic-self:{external_message_id}"
        identity = {
            "provider": "onebot",
            "external_group_id": group,
            "external_user_id": member,
            "group_openid": group,
            "member_openid": member,
        }
        async with SessionLocal() as session:
            records = [
                ViolationRecord(
                    **identity,
                    message_id=external_message_id + ("" if i == 0 else "-second"),
                    category="ad",
                    confidence=0.95,
                    message_snapshot_json='{"text":"合成测试内容"}',
                )
                for i in range(2)
            ]
            session.add_all(records)
            await session.flush()
            violation_ids = (records[0].id, records[1].id)
            cases = [
                Case(
                    **identity,
                    case_no=f"synthetic-{suffix[:16]}-{i}",
                    status=status,
                    violation_ids_json=json.dumps(violation_ids),
                )
                for i, status in enumerate(("CLOSED", "PENDING_REVIEW"))
            ]
            session.add_all(cases)
            await session.flush()
            for record in records:
                record.case_id = cases[1].id
            session.add(
                ShadowDecision(
                    **identity,
                    message_id=internal_message_id,
                    external_message_id=external_message_id,
                    kind="text",
                    verdict="violation_high",
                    category="ad",
                    confidence=0.95,
                )
            )
            for provider in ("onebot", "qq_official"):
                session.add(
                    ProviderGroupSettings(
                        provider=provider,
                        external_group_id=group,
                        action_enabled=provider == "onebot",
                    )
                )
                session.add(
                    GroupProviderRoute(
                        message_provider=provider,
                        external_group_id=group,
                        action_provider=provider,
                    )
                )
            session.add(GroupActionOwner(external_group_id=group, provider="onebot"))
            session.add(GroupAlias(group_openid=group, name="合成群备注"))
            session.add(HiddenGroup(provider="qq_official", external_group_id=group))
            await session.commit()
            return BusinessData(
                group, member, cases[0].id, cases[1].id, internal_message_id, violation_ids
            )

    return asyncio.run(seed())


async def _snapshot(data: BusinessData) -> dict[str, tuple[tuple[object, ...], ...]]:
    """比对被旧实现修改的所有相关行/列，不把真实数据库快照写入测试文件。"""
    models_and_keys = (
        (Case, Case.group_openid),
        (ViolationRecord, ViolationRecord.group_openid),
        (ShadowDecision, ShadowDecision.group_openid),
        (ProviderGroupSettings, ProviderGroupSettings.external_group_id),
        (GroupProviderRoute, GroupProviderRoute.external_group_id),
        (GroupActionOwner, GroupActionOwner.external_group_id),
        (GroupAlias, GroupAlias.group_openid),
        (HiddenGroup, HiddenGroup.external_group_id),
    )
    result = {}
    async with SessionLocal() as session:
        for model, key in models_and_keys:
            table = model.__table__
            rows = await session.execute(
                select(table).where(key == data.group).order_by(*table.primary_key.columns)
            )
            result[table.name] = tuple(tuple(row) for row in rows)
    return result


def _login(client: TestClient) -> str:
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return auth.csrf_token(client.cookies[auth.SESSION_COOKIE])


def _target(target: str, data: BusinessData) -> tuple[str, dict[str, str]]:
    if target == "case":
        return "/admin/cases/batch-delete", {"case_ids": str(data.closed_case_id)}
    return "/admin/groups/delete", {"provider": target, "group_openid": data.group}


@pytest.mark.parametrize("target", ["case", "onebot"])
def test_disabled_delete_routes_still_require_login_and_csrf(
    client: TestClient, business_data: BusinessData, target: str
) -> None:
    path, form = _target(target, business_data)
    before = asyncio.run(_snapshot(business_data))
    assert client.post(path, data=form).status_code == 401
    _login(client)
    assert client.post(path, data=form).status_code == 403
    assert client.post(path, data={**form, "csrf": "invalid"}).status_code == 403
    assert asyncio.run(_snapshot(business_data)) == before


@pytest.mark.parametrize("target", ["case", "onebot", "qq_official"])
def test_disabled_delete_preserves_shared_evidence_and_feedback_mapping(
    client: TestClient, business_data: BusinessData, target: str
) -> None:
    data = business_data
    path, form = _target(target, data)
    csrf = _login(client)
    before = asyncio.run(_snapshot(data))
    response = client.post(path, data={**form, "csrf": csrf}, follow_redirects=False)
    assert response.status_code == 200
    assert "已停用" in response.text
    assert asyncio.run(_snapshot(data)) == before

    async def verify_correction() -> None:
        async with SessionLocal() as session:
            pending = await session.get(Case, data.pending_case_id)
            assert pending is not None and pending.status == "PENDING_REVIEW"
            assert json.loads(pending.violation_ids_json) == list(data.violation_ids)
            assert await resolve_action_provider(session, "onebot", data.group) == "onebot"
            assert (
                await count_active_violations(session, data.group, data.member, provider="onebot")
                == 2
            )
            feedback = await record_feedback(
                session, data.internal_message_id, "false_positive", "ad", "synthetic-admin"
            )
            assert feedback.provider == "onebot"
            assert (
                await count_active_violations(session, data.group, data.member, provider="onebot")
                == 1
            )
            corrected = await session.get(ViolationRecord, data.violation_ids[0])
            other = await session.get(ViolationRecord, data.violation_ids[1])
            assert corrected is not None and corrected.revoked is True
            assert other is not None and other.revoked is False

    asyncio.run(verify_correction())


def test_delete_forms_are_absent_for_cases_and_visible_or_hidden_groups(
    client: TestClient, business_data: BusinessData
) -> None:
    _login(client)
    before = asyncio.run(_snapshot(business_data))
    response = client.get("/admin/")
    assert response.status_code == 200
    assert "/admin/cases/batch-delete" not in response.text
    for page in ("/admin/groups", "/admin/groups?show_hidden=1"):
        response = client.get(page)
        assert response.status_code == 200
        assert "/admin/groups/delete" not in response.text
    assert asyncio.run(_snapshot(business_data)) == before
