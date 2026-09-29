"""Long-running resource and admin-alias regressions; synthetic I/O only."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from app.actions.orchestrator import orchestrate_actions
from app.adapters.qq_official.actions import OfficialActionAdapter
from app.adapters.qq_official.auth import TokenManager
from app.config import Settings
from app.core.contracts import Sender, StandardMessage
from app.db import SessionLocal
from app.models import GroupAlias, ProviderGroupSettings
from app.moderation.decision import ModerationDecision
from app.runtime import onebot_actions, onebot_wiring
from sqlalchemy.dialects.sqlite import Insert
from sqlalchemy.ext.asyncio import AsyncSession


def _group_id() -> str:
    return str(910000000 + uuid.uuid4().int % 9000000)


@pytest.mark.asyncio
async def test_auto_group_name_preserves_admin_write_during_api_wait(monkeypatch):
    group_id = _group_id()

    async def fake_call(action, params):
        assert action == "get_group_info"
        async with SessionLocal() as admin_session:
            admin_session.add(GroupAlias(group_openid=group_id, name="管理员保存的备注"))
            await admin_session.commit()
        return {"status": "ok", "retcode": 0, "data": {"group_name": "接口群名称"}}

    monkeypatch.setattr(onebot_actions.onebot_action_hub, "call", fake_call)
    assert await onebot_wiring._autoname_group_task(group_id)
    async with SessionLocal() as check:
        assert (await check.get(GroupAlias, group_id)).name == "管理员保存的备注"


@pytest.mark.asyncio
async def test_auto_group_name_skips_existing_alias_without_calling_api(monkeypatch):
    group_id = _group_id()
    async with SessionLocal() as session:
        session.add(GroupAlias(group_openid=group_id, name="现有备注"))
        await session.commit()

    async def forbidden_call(*args, **kwargs):
        pytest.fail("An existing alias must not trigger a group-info request")

    monkeypatch.setattr(onebot_actions.onebot_action_hub, "call", forbidden_call)
    assert await onebot_wiring._autoname_group_task(group_id)
    async with SessionLocal() as check:
        assert (await check.get(GroupAlias, group_id)).name == "现有备注"


@pytest.mark.asyncio
async def test_auto_group_name_insert_conflict_preserves_concurrent_admin(monkeypatch):
    group_id = _group_id()
    original_execute = AsyncSession.execute
    inserted_by_admin = False

    async def execute(self, statement, *args, **kwargs):
        nonlocal inserted_by_admin
        if (
            not inserted_by_admin
            and isinstance(statement, Insert)
            and statement.table.name == GroupAlias.__tablename__
        ):
            inserted_by_admin = True
            async with SessionLocal() as admin_session:
                admin_session.add(GroupAlias(group_openid=group_id, name="并发人工备注"))
                await admin_session.commit()
        return await original_execute(self, statement, *args, **kwargs)

    async def fake_call(*args, **kwargs):
        return {"status": "ok", "retcode": 0, "data": {"group_name": "接口群名称"}}

    monkeypatch.setattr(AsyncSession, "execute", execute)
    monkeypatch.setattr(onebot_actions.onebot_action_hub, "call", fake_call)
    assert await onebot_wiring._autoname_group_task(group_id)
    assert inserted_by_admin
    async with SessionLocal() as check:
        assert (await check.get(GroupAlias, group_id)).name == "并发人工备注"


def _track_http_clients(monkeypatch, handler):
    original = httpx.AsyncClient
    clients = []

    def create(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        client = original(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create)
    return clients


def _settings():
    return Settings(
        app_env="prod",
        admin_password="synthetic-admin-password",
        qq_app_id="synthetic-app",
        qq_app_secret="synthetic-secret",
        action_mode="OFFICIAL",
        _env_file=None,
    )


async def _run_chain(*, official_client=None):
    group = "G_OWNED_HTTP_" + uuid.uuid4().hex
    msg = StandardMessage(
        message_id="MSG_" + uuid.uuid4().hex,
        group_openid=group,
        sender=Sender(member_openid="SYNTHETIC_MEMBER"),
        text="synthetic violation",
    )
    decision = ModerationDecision(
        message_id=msg.message_id,
        group_openid=group,
        sender_member_openid=msg.sender.member_openid,
        sender_role="member",
        verdict="violation_high",
        category="ad",
        confidence=0.95,
        recommended_actions=["recall"],
        reason="synthetic high-confidence violation",
    )
    async with SessionLocal() as session:
        session.add(
            ProviderGroupSettings(
                provider="qq_official", external_group_id=group, action_enabled=True
            )
        )
        await session.commit()
        return await orchestrate_actions(
            session, msg, decision, official_client=official_client, settings=_settings()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["success", "failed", "timeout", "cancelled"])
async def test_default_official_chain_closes_owned_clients(monkeypatch, result):
    in_request = asyncio.Event()
    paths = []

    async def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "synthetic", "expires_in": 7200})
        in_request.set()
        if result == "cancelled":
            await asyncio.Event().wait()
        if result == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(500 if result == "failed" else 200, json={})

    clients = _track_http_clients(monkeypatch, handler)
    task = asyncio.create_task(_run_chain())
    try:
        if result == "cancelled":
            await asyncio.wait_for(in_request.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            intents = await task
            assert len(intents) == 1
            assert all(
                i.status == ("SUCCEEDED" if result == "success" else "FAILED") for i in intents
            )
        assert len(clients) == 2
        assert all(c.is_closed for c in clients)
        assert sum(path.endswith("getAppAccessToken") for path in paths) == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_injected_official_adapter_is_left_open_by_orchestrator(monkeypatch):
    async def handler(request):
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "synthetic", "expires_in": 7200})
        return httpx.Response(200, json={})

    clients = _track_http_clients(monkeypatch, handler)
    token_manager = TokenManager("synthetic-app", "synthetic-secret")
    adapter = OfficialActionAdapter(token_manager)
    try:
        intents = await _run_chain(official_client=adapter)
        assert len(intents) == 1 and all(i.status == "SUCCEEDED" for i in intents)
        assert len(clients) == 2 and all(not c.is_closed for c in clients)
    finally:
        await adapter.aclose()
        await token_manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("inject_action_client", [False, True])
async def test_adapter_close_preserves_injected_resource_ownership(
    monkeypatch, inject_action_client
):
    async def forbidden_network(request):
        pytest.fail("closing resources must not make a request")

    clients = _track_http_clients(monkeypatch, forbidden_network)
    token_manager = TokenManager("synthetic-app", "synthetic-secret")
    injected_client = httpx.AsyncClient() if inject_action_client else None
    adapter = OfficialActionAdapter(token_manager, client=injected_client)
    try:
        await adapter.aclose()
        assert len(clients) == 2
        assert not clients[0].is_closed, "injected token manager belongs to caller"
        assert clients[1].is_closed is (not inject_action_client)
    finally:
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_default_client_closes_when_strike_persistence_fails(monkeypatch):
    from app.actions import orchestrator

    async def forbidden_network(request):
        pytest.fail("failed strike persistence must precede external calls")

    async def failing_strike(*args, **kwargs):
        raise RuntimeError("synthetic persistence failure")

    clients = _track_http_clients(monkeypatch, forbidden_network)
    monkeypatch.setattr(orchestrator, "record_violation", failing_strike)
    try:
        with pytest.raises(RuntimeError, match="synthetic persistence failure"):
            await _run_chain()
        assert len(clients) == 2 and all(c.is_closed for c in clients)
    finally:
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_owned_token_manager_closes_even_if_action_client_close_raises(monkeypatch):
    async def forbidden_network(request):
        pytest.fail("closing resources must not make a request")

    clients = _track_http_clients(monkeypatch, forbidden_network)
    token_manager = TokenManager("synthetic-app", "synthetic-secret")
    adapter = OfficialActionAdapter(token_manager, owns_token_manager=True)
    original_close = clients[1].aclose

    async def failing_close():
        await original_close()
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(clients[1], "aclose", failing_close)
    try:
        with pytest.raises(RuntimeError, match="synthetic cleanup failure"):
            await adapter.aclose()
        assert len(clients) == 2 and all(c.is_closed for c in clients)
    finally:
        await original_close()
        await clients[0].aclose()
