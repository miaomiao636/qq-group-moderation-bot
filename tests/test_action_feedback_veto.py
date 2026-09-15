"""Human correction must win before each remaining external punishment action."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.actions import orchestrator
from app.actions.orchestrator import orchestrate_actions
from app.cases.models import ViolationRecord
from app.config import Settings
from app.core.contracts import ActionResult, Sender, StandardMessage
from app.core.routing import upsert_group_route
from app.db import Base
from app.models import AdminAudit, ProviderGroupSettings
from app.moderation.decision import ModerationDecision
from app.moderation.feedback import FeedbackRecord, record_feedback
from app.runtime.models import ShadowDecision
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine


class _FakeClient:
    def __init__(self, correction: Callable[[], Awaitable[None]] | None = None) -> None:
        self.calls: list[str] = []
        self.correction = correction

    async def recall(self, *args, **kwargs) -> ActionResult:
        self.calls.append("recall")
        if self.correction:
            await self.correction()
        return ActionResult(action="recall", ok=True, attempts=1)

    async def mute(self, *args, **kwargs) -> ActionResult:
        self.calls.append("mute")
        return ActionResult(action="mute", ok=True, attempts=1)

    async def warn(self, *args, **kwargs) -> ActionResult:
        self.calls.append("warn")
        return ActionResult(action="warn", ok=True, attempts=1)


@pytest.fixture
async def veto_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'feedback-veto.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            session.add(
                ProviderGroupSettings(
                    provider="onebot", external_group_id="42", action_enabled=True
                )
            )
            session.add(
                ShadowDecision(
                    message_id="onebot:1:101",
                    external_message_id="101",
                    provider="onebot",
                    external_group_id="42",
                    external_user_id="84",
                    group_openid="42",
                    member_openid="84",
                    verdict="violation_high",
                )
            )
            await upsert_group_route(
                session, "42", message_provider="onebot", action_provider="onebot"
            )
        yield engine
    finally:
        await engine.dispose()


async def _run(
    engine: AsyncEngine, client: _FakeClient, *, message_id: str = "101", user_id: str = "84"
):
    message = StandardMessage(
        message_id=message_id,
        provider="onebot",
        external_group_id="42",
        external_user_id=user_id,
        sender=Sender(member_openid=user_id),
        text="脱敏测试",
    )
    decision = ModerationDecision(
        message_id=message_id,
        provider="onebot",
        external_group_id="42",
        external_user_id=user_id,
        verdict="violation_high",
        confidence=0.99,
        recommended_actions=["recall", "mute", "warn"],
    )
    settings = Settings(
        _env_file=None,
        app_env="prod",
        admin_password="test-admin-password",
        action_mode="OFFICIAL",
        onebot_ws_enabled=True,
        onebot_actions_enabled=True,
        onebot_access_token="test-onebot-token",
        onebot_self_id="1",
        onebot_action_stage="full",
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await orchestrate_actions(
            session, message, decision, onebot_client=client, settings=settings
        )


@pytest.mark.parametrize("correction_kind", ["feedback", "revoked"])
async def test_correction_after_recall_stops_mute_and_warn(
    veto_engine: AsyncEngine, correction_kind: str
) -> None:
    async def correct() -> None:
        async with AsyncSession(veto_engine) as session:
            if correction_kind == "feedback":
                await record_feedback(session, "onebot:1:101", "false_positive", "other", "human")
            else:
                await session.execute(update(ViolationRecord).values(revoked=True))
                await session.commit()

    client = _FakeClient(correct)
    intents = await _run(veto_engine, client)

    assert client.calls == ["recall"]
    assert [intent.status for intent in intents] == ["SUCCEEDED", "SKIPPED"]


@pytest.mark.parametrize("label", ["false_positive", "confirmed_normal"])
async def test_preexisting_negative_feedback_blocks_new_strike(
    veto_engine: AsyncEngine, label: str
) -> None:
    async with AsyncSession(veto_engine) as session:
        await record_feedback(session, "onebot:1:101", label, "other", "human")
    client = _FakeClient()

    intents = await _run(veto_engine, client)

    assert client.calls == []
    assert [intent.status for intent in intents] == ["SKIPPED"]
    async with AsyncSession(veto_engine) as session:
        assert list(await session.scalars(select(ViolationRecord))) == []


@pytest.mark.parametrize(
    "mismatch", ["provider", "external_group_id", "external_user_id", "external_message_id"]
)
async def test_negative_feedback_never_guesses_cross_identity(
    veto_engine: AsyncEngine, mismatch: str
) -> None:
    async with AsyncSession(veto_engine) as session:
        shadow = await session.scalar(select(ShadowDecision))
        assert shadow is not None
        setattr(shadow, mismatch, "qq_official" if mismatch == "provider" else "999")
        session.add(
            FeedbackRecord(
                message_id="onebot:1:101",
                provider=shadow.provider,
                external_group_id=shadow.external_group_id,
                external_user_id=shadow.external_user_id,
                label="false_positive",
                operator="human",
            )
        )
        await session.commit()
    client = _FakeClient()

    await _run(veto_engine, client)

    assert client.calls == ["recall", "mute", "warn"]


async def test_latest_explicit_positive_supersedes_negative_without_prior_strike(
    veto_engine: AsyncEngine,
) -> None:
    async with AsyncSession(veto_engine) as session:
        await record_feedback(session, "onebot:1:101", "false_positive", "other", "human")
        await record_feedback(session, "onebot:1:101", "confirmed_violation", "ad", "human")
    client = _FakeClient()

    await _run(veto_engine, client)

    assert client.calls == ["recall", "mute", "warn"]


async def test_latest_feedback_follows_insertion_order_after_clock_rollback(
    veto_engine: AsyncEngine,
) -> None:
    async with AsyncSession(veto_engine) as session:
        for label, year in (("confirmed_violation", 2030), ("false_positive", 2020)):
            session.add(
                FeedbackRecord(
                    message_id="onebot:1:101",
                    provider="onebot",
                    external_group_id="42",
                    external_user_id="84",
                    label=label,
                    operator="human",
                    created_at=datetime(year, 1, 1, tzinfo=UTC),
                )
            )
            await session.commit()
    client = _FakeClient()

    await _run(veto_engine, client)

    assert client.calls == []
    async with AsyncSession(veto_engine) as session:
        assert list(await session.scalars(select(ViolationRecord))) == []


class _DelayedClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.durations: list[int] = []

    async def recall(self, group: str, message_id: str, **kwargs) -> ActionResult:
        if message_id == "101":
            self.first_started.set()
            await self.release_first.wait()
        return await super().recall(group, message_id, **kwargs)

    async def mute(self, group: str, user: str, seconds: int, **kwargs) -> ActionResult:
        self.durations.append(seconds)
        return await super().mute(group, user, seconds, **kwargs)


async def test_delayed_first_chain_cannot_shorten_second_mute(veto_engine: AsyncEngine) -> None:
    client = _DelayedClient()
    first = asyncio.create_task(_run(veto_engine, client))
    await asyncio.wait_for(client.first_started.wait(), timeout=10)
    second = asyncio.create_task(_run(veto_engine, client, message_id="102"))
    try:
        # Old implementation completes the second chain here, producing 24h→1h.
        # Serialized implementation waits until first.release_first below.
        await asyncio.wait({second}, timeout=0.2)
    finally:
        client.release_first.set()
        await asyncio.gather(first, second)
    assert client.durations == [3600, 86400]
    assert client.calls.count("warn") == 1
    assert not orchestrator._member_action_chains


async def test_different_members_do_not_block_each_other(veto_engine: AsyncEngine) -> None:
    client = _DelayedClient()
    first = asyncio.create_task(_run(veto_engine, client))
    await asyncio.wait_for(client.first_started.wait(), timeout=10)
    try:
        second = await asyncio.wait_for(
            _run(veto_engine, client, message_id="102", user_id="85"), timeout=10
        )
        assert [intent.status for intent in second] == ["SUCCEEDED"] * 3
    finally:
        client.release_first.set()
        await first
    assert not orchestrator._member_action_chains


async def test_member_chain_timeout_is_audited_and_sends_no_second_action(
    veto_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "MEMBER_ACTION_CHAIN_WAIT_SECONDS", 0.03, raising=False)
    client = _DelayedClient()
    first = asyncio.create_task(_run(veto_engine, client))
    await asyncio.wait_for(client.first_started.wait(), timeout=10)
    try:
        second = await _run(veto_engine, client, message_id="102")
        assert [intent.status for intent in second] == ["SKIPPED"]
        assert client.calls == []  # First recall is held; second never reached client.
        async with AsyncSession(veto_engine) as session:
            audit = await session.scalar(
                select(AdminAudit).where(AdminAudit.action == "member_action_chain_timeout")
            )
            assert audit is not None and audit.target_id == "102"
            assert "record_only" in audit.detail_json
            assert len(list(await session.scalars(select(ViolationRecord)))) == 1
    finally:
        client.release_first.set()
        await first
    assert not orchestrator._member_action_chains


async def test_cancelled_member_chain_waiter_releases_reference(veto_engine: AsyncEngine) -> None:
    client = _DelayedClient()
    first = asyncio.create_task(_run(veto_engine, client))
    await asyncio.wait_for(client.first_started.wait(), timeout=10)
    waiter = asyncio.create_task(_run(veto_engine, client, message_id="102"))
    try:
        await asyncio.wait({waiter}, timeout=0.05)
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
    finally:
        client.release_first.set()
        await first
    assert not orchestrator._member_action_chains
    await _run(veto_engine, client, message_id="103")
    assert client.durations == [3600, 86400]
    assert not orchestrator._member_action_chains


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_exception_or_cancellation_releases_owned_chain(
    veto_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, error_type
) -> None:
    async def fail(*args, **kwargs):
        raise error_type()

    original = orchestrator._orchestrate_member_actions
    monkeypatch.setattr(orchestrator, "_orchestrate_member_actions", fail)
    with pytest.raises(error_type):
        await _run(veto_engine, _FakeClient())
    assert not orchestrator._member_action_chains
    monkeypatch.setattr(orchestrator, "_orchestrate_member_actions", original)
    client = _FakeClient()
    await _run(veto_engine, client)
    assert client.calls == ["recall", "mute", "warn"]
    assert not orchestrator._member_action_chains
