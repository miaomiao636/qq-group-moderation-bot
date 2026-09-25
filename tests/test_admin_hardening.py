"""R-105-C: authority, immutable human approval, identity and stop regressions."""

from __future__ import annotations

import re
import uuid
from contextlib import closing

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def admin_app(monkeypatch):
    from app.config import get_settings
    from app.main import app

    monkeypatch.setenv("AGENT_API_TOKEN", "test-r105-writer")
    monkeypatch.setenv("AGENT_API_READ_TOKEN", "test-r105-reader")
    monkeypatch.setenv(
        "AGENT_API_WRITE_SCOPES",
        "project:read,settings:write,actions:enable,routing:write,emergency:stop",
    )
    get_settings.cache_clear()
    yield app
    get_settings.cache_clear()


WRITE = {"Authorization": "Bearer test-r105-writer"}
READ = {"Authorization": "Bearer test-r105-reader"}


def login_human(client):
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    html = client.get("/admin/groups").text
    match = re.search(r'name="?csrf"? value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_read_only_token_cannot_write(admin_app):
    with TestClient(admin_app) as client:
        assert client.get("/admin/api/status", headers=READ).status_code == 200
        assert (
            client.post(
                "/admin/api/groups/read-only/settings",
                headers=READ,
                params={"provider": "qq_official", "moderation_enabled": "true"},
            ).status_code
            == 403
        )


def test_real_session_expires(monkeypatch):
    from app.web import auth

    now = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    token = auth.login("admin", "test-admin-pass")
    csrf = auth.csrf_token(token)
    assert auth.is_valid(token)
    now[0] += 86401
    assert not auth.is_valid(token)
    assert not auth.validate_csrf(token, csrf)


def test_agent_cannot_self_approve_and_plan_binds_every_parameter(admin_app):
    group = "approval-" + uuid.uuid4().hex
    params = {"provider": "qq_official", "action_enabled": "true", "name": "preview"}
    with TestClient(admin_app) as agent, closing(TestClient(admin_app)) as human:
        preview = agent.post(f"/admin/api/groups/{group}/settings", params=params, headers=WRITE)
        assert preview.status_code == 202
        plan = preview.json()["confirmation_token"]
        assert (
            agent.post(
                f"/admin/api/groups/{group}/settings",
                params={**params, "confirm_token": plan},
                headers=WRITE,
            ).status_code
            == 403
        )
        assert agent.post(f"/admin/plans/{plan}/approve", headers=WRITE).status_code == 401
        csrf = login_human(human)
        assert (
            human.post(
                f"/admin/plans/{plan}/approve",
                data={"csrf": csrf},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            agent.post(
                f"/admin/api/groups/{group}/settings",
                params={**params, "name": "tampered", "confirm_token": plan},
                headers=WRITE,
            ).status_code
            == 403
        )
        approved = agent.post(
            f"/admin/api/groups/{group}/settings",
            params={**params, "confirm_token": plan},
            headers=WRITE,
        )
        assert approved.status_code == 200
        assert approved.json()["name"] == "preview"
        assert (
            agent.post(
                f"/admin/api/groups/{group}/settings",
                params={**params, "confirm_token": plan},
                headers=WRITE,
            ).status_code
            == 403
        )


@pytest.mark.asyncio
async def test_group_settings_do_not_leak_between_providers():
    from app.core.group_settings import get_or_create_group_settings, is_action_enabled
    from app.db import SessionLocal

    group = "identity-" + uuid.uuid4().hex
    async with SessionLocal() as session:
        gs = await get_or_create_group_settings(session, group, provider="qq_official")
        gs.action_enabled = True
        await session.commit()
        assert await is_action_enabled(session, group, provider="qq_official")
        assert not await is_action_enabled(session, group, provider="onebot")


@pytest.mark.asyncio
async def test_existing_duplicate_routes_fail_closed_then_explicit_owner_wins():
    from app.core.routing import GroupProviderRoute, resolve_action_provider, upsert_group_route
    from app.db import SessionLocal

    group = "routes-" + uuid.uuid4().hex
    async with SessionLocal() as session:
        session.add_all(
            [
                GroupProviderRoute(message_provider=p, external_group_id=group, action_provider=p)
                for p in ("onebot", "qq_official")
            ]
        )
        await session.commit()
        assert await resolve_action_provider(session, "onebot", group) is None
        assert await resolve_action_provider(session, "qq_official", group) is None
        await upsert_group_route(
            session, group, message_provider="onebot", action_provider="onebot"
        )
        assert await resolve_action_provider(session, "onebot", group) == "onebot"
        assert await resolve_action_provider(session, "qq_official", group) is None


@pytest.mark.asyncio
async def test_shared_stop_after_recall_prevents_next_message_recall():
    from app.actions.orchestrator import orchestrate_actions
    from app.core.emergency_stop import set_emergency_stop
    from app.db import SessionLocal

    from tests.test_action_orchestrator import (
        FakeOfficialClient,
        _enable_actions,
        _high_decision,
        _msg,
        _official_settings,
    )

    class StopAfterRecall(FakeOfficialClient):
        async def recall(self, *args, **kwargs):
            result = await super().recall(*args, **kwargs)
            async with SessionLocal() as control:
                await set_emergency_stop(control, True, actor="test-human")
            return result

    client = StopAfterRecall()
    msg = _msg("stop-" + uuid.uuid4().hex, "user")
    try:
        async with SessionLocal() as session:
            await _enable_actions(session, msg.external_group_id)
            results = await orchestrate_actions(
                session,
                msg,
                _high_decision(msg),
                official_client=client,
                settings=_official_settings(),
            )
            next_message = _msg(msg.external_group_id, "other")
            next_results = await orchestrate_actions(
                session,
                next_message,
                _high_decision(next_message),
                official_client=client,
                settings=_official_settings(),
            )
        assert [action for action, _ in client.calls] == ["recall"]
        assert [intent.status for intent in results] == ["SUCCEEDED"]
        assert [intent.status for intent in next_results] == ["SKIPPED"]
    finally:
        async with SessionLocal() as session:
            await set_emergency_stop(session, False, actor="test-human")


def test_scopes_are_checked_for_action_enable(admin_app, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("AGENT_API_WRITE_SCOPES", "project:read,settings:write")
    get_settings.cache_clear()
    with TestClient(admin_app) as client:
        assert (
            client.post(
                "/admin/api/groups/scope-test/settings",
                params={"provider": "onebot", "action_enabled": True},
                headers=WRITE,
            ).status_code
            == 403
        )


def test_unknown_group_provider_is_explicit_error(admin_app):
    with TestClient(admin_app) as client:
        assert (
            client.post(
                "/admin/api/groups/unknown-provider/settings",
                params={"moderation_enabled": True},
                headers=WRITE,
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/admin/api/groups/unknown-provider/settings",
                params={"provider": "invented"},
                headers=WRITE,
            ).status_code
            == 422
        )


@pytest.mark.asyncio
async def test_plan_expiry_requestor_and_single_atomic_consumer():
    import asyncio
    from datetime import UTC, datetime, timedelta

    from app.db import SessionLocal
    from app.web.agent_confirm import approve_confirmation, claim_confirmation, create_confirmation

    async with SessionLocal() as session:
        plan = await create_confirmation(
            session,
            action="group_settings",
            params={"version": 1},
            expected_state={},
            requestor="agent:original",
        )
        plan_id = plan.id
        assert not await approve_confirmation(session, plan_id, human="agent:original")
        assert await approve_confirmation(session, plan_id, human="human:admin")
        assert await claim_confirmation(session, plan_id, requestor="agent:thief") is None

    async def claim():
        async with SessionLocal() as session:
            return await claim_confirmation(session, plan_id, requestor="agent:original")

    results = await asyncio.gather(claim(), claim())
    assert sum(result is not None for result in results) == 1
    async with SessionLocal() as session:
        expired = await create_confirmation(
            session, action="group_settings", params={}, expected_state={}, requestor="agent:test"
        )
        expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        assert not await approve_confirmation(session, expired.id, human="human:admin")


def test_settings_changed_since_preview_require_new_human_approval(admin_app):
    group = "stale-" + uuid.uuid4().hex
    params = {"provider": "qq_official", "action_enabled": True}
    with TestClient(admin_app) as agent:
        preview = agent.post(
            f"/admin/api/groups/{group}/settings", params=params, headers=WRITE
        ).json()
        from tests.test_agent_confirmation import _approve

        _approve(admin_app, preview["plan_id"])
        changed = agent.post(
            f"/admin/api/groups/{group}/settings",
            params={"provider": "qq_official", "name": "newer"},
            headers=WRITE,
        )
        assert changed.status_code == 200
        assert (
            agent.post(
                f"/admin/api/groups/{group}/settings",
                params={**params, "confirm_token": preview["plan_id"]},
                headers=WRITE,
            ).status_code
            == 409
        )


def test_emergency_stop_api_persists_and_resume_needs_fresh_human_plan(admin_app):
    import asyncio

    from app.core.emergency_stop import emergency_stop_active, set_emergency_stop
    from app.db import SessionLocal

    async def active():
        async with SessionLocal() as session:
            return await emergency_stop_active(session)

    try:
        with TestClient(admin_app) as agent, closing(TestClient(admin_app)) as human:
            assert agent.post("/admin/api/emergency-stop", headers=READ).status_code == 403
            assert agent.post("/admin/api/emergency-stop", headers=WRITE).status_code == 200
            assert asyncio.run(active()) is True
            csrf = login_human(human)
            response = human.post(
                "/admin/emergency-resume", data={"csrf": csrf}, follow_redirects=False
            )
            plan_url = response.headers["location"]
            assert (
                human.post(
                    plan_url + "/approve", data={"csrf": csrf}, follow_redirects=False
                ).status_code
                == 303
            )
            assert agent.post("/admin/api/emergency-stop", headers=WRITE).status_code == 200
            assert human.post(plan_url + "/execute", data={"csrf": csrf}).status_code == 409
            assert asyncio.run(active()) is True
    finally:

        async def reset():
            async with SessionLocal() as session:
                await set_emergency_stop(session, False, actor="test")

        asyncio.run(reset())


@pytest.mark.asyncio
async def test_legacy_rule_scope_marks_provider_collision_ambiguous():
    from app.core.group_settings import ambiguous_legacy_rule_scope, get_or_create_group_settings
    from app.db import SessionLocal

    group = "scope-" + uuid.uuid4().hex
    async with SessionLocal() as session:
        await get_or_create_group_settings(session, group, provider="onebot")
        await session.commit()
        assert not await ambiguous_legacy_rule_scope(session, group, "onebot")
        assert await ambiguous_legacy_rule_scope(session, group, "qq_official")


@pytest.mark.asyncio
async def test_ambiguous_group_cannot_apply_legacy_rules_or_create_actions():
    import json

    from app.core.group_settings import get_or_create_group_settings
    from app.db import SessionLocal
    from app.moderation.dynamic_rules import add_rule_item, create_rule_draft, publish_rule_version
    from app.runtime.pipeline import run_pipeline

    group = "pipeline-collision-" + uuid.uuid4().hex
    word = "仅属于另一个来源的测试规则" + uuid.uuid4().hex
    async with SessionLocal() as session:
        await get_or_create_group_settings(session, group, provider="onebot")
        await session.commit()
        draft = await create_rule_draft(session, scope="group", scope_key=group, name="ambiguous")
        await add_rule_item(
            session, draft.id, item_type="keyword", pattern=word, category="ad", weight=0.99
        )
        await publish_rule_version(session, draft.id, operator="test")
        record = await run_pipeline(
            {
                "id": "collision-" + uuid.uuid4().hex,
                "group_openid": group,
                "author": {"member_openid": "member", "member_role": "member", "bot": False},
                "content": word,
                "attachments": [],
                "timestamp": "2026-09-10T00:00:00+08:00",
            },
            session,
        )
        assert record is not None
        assert record.verdict == "record_only"
        assert "歧义" in record.reason
        detail = json.loads(record.detail_json)
        assert draft.id not in detail["rule_version_ids"]
        assert detail["recommended_actions"] == []


@pytest.mark.asyncio
async def test_stop_survives_new_interpreter():
    import asyncio
    import os
    import subprocess
    import sys

    from app.config import PROJECT_ROOT
    from app.core.emergency_stop import set_emergency_stop
    from app.db import SessionLocal

    code = """
import asyncio
from app.db import SessionLocal, engine
from app.core.emergency_stop import emergency_stop_active
async def main():
    async with SessionLocal() as session:
        assert await emergency_stop_active(session)
    await engine.dispose()
asyncio.run(main())
"""
    try:
        async with SessionLocal() as session:
            await set_emergency_stop(session, True, actor="test")
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
    finally:
        async with SessionLocal() as session:
            await set_emergency_stop(session, False, actor="test")


@pytest.mark.parametrize(
    "scopes, expected_status",
    [
        ("project:read,settings:write", 403),
        ("project:read, settings:write, actions:enable, routing:write", 202),
    ],
)
def test_settings_scope_cannot_reactivate_previously_armed_group(
    admin_app, monkeypatch, scopes, expected_status
):
    import asyncio

    from app.config import get_settings
    from app.db import SessionLocal
    from app.models import ProviderGroupSettings

    group = "reactivate-" + uuid.uuid4().hex

    async def seed():
        async with SessionLocal() as session:
            session.add(
                ProviderGroupSettings(
                    provider="onebot",
                    external_group_id=group,
                    action_enabled=True,
                    moderation_enabled=False,
                )
            )
            await session.commit()

    asyncio.run(seed())
    monkeypatch.setenv("AGENT_API_WRITE_SCOPES", scopes)
    get_settings.cache_clear()
    with TestClient(admin_app) as client:
        response = client.post(
            f"/admin/api/groups/{group}/settings",
            headers=WRITE,
            params={"provider": "onebot", "moderation_enabled": True},
        )
        assert response.status_code == expected_status


@pytest.mark.asyncio
async def test_same_pending_intent_has_one_atomic_sender():
    import asyncio

    from app.actions.orchestrator import ActionIntent, _create_intent, _execute_intent
    from app.db import SessionLocal

    from tests.test_action_orchestrator import FakeOfficialClient, _enable_actions, _msg

    msg = _msg("claim-" + uuid.uuid4().hex, "member")
    client = FakeOfficialClient()
    async with SessionLocal() as setup:
        await _enable_actions(setup, msg.external_group_id)
        intent = await _create_intent(setup, msg, "recall", {}, "test")
        intent_id = intent.id
    async with SessionLocal() as first, SessionLocal() as second:
        a = await first.get(ActionIntent, intent_id)
        b = await second.get(ActionIntent, intent_id)
        results = await asyncio.gather(
            _execute_intent(first, client, a), _execute_intent(second, client, b)
        )
    assert len(client.calls) == 1
    assert sum(result is not None for result in results) == 1
