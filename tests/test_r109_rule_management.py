"""Rule-management requests preserve reviewed snapshots and explicit intent.

Synthetic messages and migrated temporary SQLite only; TestClient does not run
lifespan workers, and no model, QQ or other external API is called.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.db import SessionLocal
from app.moderation.dynamic_rules import (
    RuleItem,
    RuleVersion,
    add_rule_item,
    create_rule_draft,
    load_cached_active_snapshot,
    publish_rule_version,
    set_rule_item_enabled,
)
from app.moderation.feedback import (
    RuleCandidate,
    RuleCandidateExample,
    copy_candidate_to_draft,
    mine_rule_candidates,
    purge_dismissed_rule_candidates,
    record_feedback,
)
from app.web import auth
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from app.main import app

    http = TestClient(app)
    response = http.post(
        "/admin/login",
        data={"username": "admin", "password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    try:
        yield http
    finally:
        auth.logout(http.cookies[auth.SESSION_COOKIE])
        http.close()


async def _seed(status: str = "ACTIVE") -> tuple[int, int, int, str]:
    group = f"r109-rules-{uuid.uuid4().hex}"
    async with SessionLocal() as session:
        version = await create_rule_draft(
            session, scope="group", scope_key=group, name="Synthetic reviewed rules"
        )
        item = await add_rule_item(
            session,
            version.id,
            item_type="keyword",
            pattern="synthetic-rule-target",
            category="ad",
            weight=0.95,
        )
        await add_rule_item(
            session,
            version.id,
            item_type="keyword",
            pattern="synthetic-rule-retained",
            category="fraud",
            weight=0.97,
        )
        if status != "DRAFT":
            await publish_rule_version(session, version.id, operator="synthetic-reviewer")
        if status == "ARCHIVED":
            version.status = status
            await session.commit()
        return version.id, item.id, version.rule_set_id, group


def _post(client: TestClient, item_id: int, action: str = "disable") -> None:
    response = client.post(
        f"/admin/rules/items/{item_id}/{action}",
        data={"csrf": auth.csrf_token(client.cookies[auth.SESSION_COOKIE])},
        follow_redirects=False,
    )
    assert response.status_code in {200, 303, 409}


async def _items(version_id: int) -> list[tuple[str, bool]]:
    async with SessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(RuleItem.pattern, RuleItem.enabled)
                    .where(RuleItem.version_id == version_id)
                    .order_by(RuleItem.id)
                )
            ).tuples()
        )


def test_disable_active_item_creates_draft_without_mutating_reviewed_version(client: TestClient):
    version_id, item_id, rule_set_id, _group = asyncio.run(_seed())
    before = asyncio.run(_items(version_id))
    _post(client, item_id)

    async def verify() -> None:
        assert await _items(version_id) == before
        async with SessionLocal() as session:
            drafts = (
                await session.scalars(
                    select(RuleVersion).where(
                        RuleVersion.rule_set_id == rule_set_id, RuleVersion.status == "DRAFT"
                    )
                )
            ).all()
        assert len(drafts) == 1
        assert await _items(drafts[0].id) == [
            ("synthetic-rule-target", False),
            ("synthetic-rule-retained", True),
        ]

    asyncio.run(verify())


@pytest.mark.parametrize("linked", [False, True])
def test_candidate_cleanup_preserves_ids_and_copied_rule_provenance(
    client: TestClient, linked: bool
):
    async def seed_candidate() -> int:
        async with SessionLocal() as session:
            candidate = RuleCandidate(
                scope_key=f"r109-candidate-{uuid.uuid4().hex}",
                item_type="keyword",
                pattern="synthetic-sensitive-candidate",
                replay_report_json='{"synthetic_private_detail":"redact me"}',
                status="DISMISSED",
                copied_version_id=501 if linked else None,
            )
            session.add(candidate)
            await session.flush()
            session.add(
                RuleCandidateExample(
                    candidate_id=candidate.id,
                    feedback_id=501,
                    message_id="synthetic-candidate-message",
                )
            )
            await session.commit()
            return candidate.id

    candidate_id = asyncio.run(seed_candidate())
    response = client.post(
        "/admin/feedback/candidates/purge-dismissed",
        data={"csrf": auth.csrf_token(client.cookies[auth.SESSION_COOKIE])},
        follow_redirects=False,
    )
    assert response.status_code == 303

    async def verify_candidate() -> None:
        async with SessionLocal() as session:
            candidate = await session.get(RuleCandidate, candidate_id)
            examples = (
                await session.scalars(
                    select(RuleCandidateExample).where(
                        RuleCandidateExample.candidate_id == candidate_id
                    )
                )
            ).all()
            assert candidate is not None, "A reviewed candidate ID must never be recycled"
            if linked:
                assert candidate.pattern == "synthetic-sensitive-candidate"
                assert candidate.copied_version_id == 501
                assert len(examples) == 1
            else:
                assert candidate.status == "PURGED"
                assert candidate.pattern == ""
                assert candidate.replay_report_json == "{}"
                assert not examples

    asyncio.run(verify_candidate())


def test_repeated_disable_draft_request_never_reenables_item(client: TestClient):
    version_id, item_id, _rule_set_id, _group = asyncio.run(_seed("DRAFT"))
    _post(client, item_id)
    _post(client, item_id)
    assert asyncio.run(_items(version_id))[0] == ("synthetic-rule-target", False)


def test_archived_version_cannot_be_edited_by_old_page(client: TestClient):
    version_id, item_id, _rule_set_id, _group = asyncio.run(_seed("ARCHIVED"))
    before = asyncio.run(_items(version_id))
    _post(client, item_id)
    assert asyncio.run(_items(version_id)) == before


def test_published_rule_stays_consistent_after_cache_expiry_until_draft_is_published(
    client: TestClient,
):
    version_id, item_id, _rule_set_id, group = asyncio.run(_seed())

    async def snapshot(now: float):
        async with SessionLocal() as session:
            return await load_cached_active_snapshot(session, group, now=now)

    before = asyncio.run(snapshot(100.0))
    _post(client, item_id)
    after = asyncio.run(snapshot(106.0))
    assert after == before
    assert version_id in after.version_ids


def test_repeated_active_request_reuses_one_change_draft(client: TestClient):
    _version_id, item_id, rule_set_id, _group = asyncio.run(_seed())
    _post(client, item_id)
    _post(client, item_id)

    async def verify() -> None:
        async with SessionLocal() as session:
            versions = (
                await session.scalars(
                    select(RuleVersion).where(RuleVersion.rule_set_id == rule_set_id)
                )
            ).all()
        assert len(versions) == 2
        assert sorted(version.status for version in versions) == ["ACTIVE", "DRAFT"]

    asyncio.run(verify())


@pytest.mark.asyncio
async def test_concurrent_active_requests_share_one_change_draft() -> None:
    _version_id, item_id, rule_set_id, _group = await _seed()

    async def request_change():
        async with SessionLocal() as session:
            return await set_rule_item_enabled(
                session, item_id, enabled=False, operator="synthetic-reviewer"
            )

    first, second = await asyncio.gather(request_change(), request_change())
    assert first.version_id == second.version_id
    assert first.item_id == second.item_id
    assert sum(result.created_draft for result in (first, second)) == 1
    async with SessionLocal() as session:
        versions = (
            await session.scalars(select(RuleVersion).where(RuleVersion.rule_set_id == rule_set_id))
        ).all()
    assert len(versions) == 2


def test_explicit_enable_is_available_and_retry_safe_for_draft(client: TestClient):
    version_id, item_id, _rule_set_id, _group = asyncio.run(_seed("DRAFT"))
    _post(client, item_id)
    _post(client, item_id, "enable")
    _post(client, item_id, "enable")
    assert asyncio.run(_items(version_id))[0] == ("synthetic-rule-target", True)


@pytest.mark.asyncio
async def test_change_draft_publication_refreshes_cache_without_editing_original() -> None:
    version_id, item_id, _rule_set_id, group = await _seed()
    async with SessionLocal() as session:
        before = await load_cached_active_snapshot(session, group)
        change = await set_rule_item_enabled(
            session, item_id, enabled=False, operator="synthetic-reviewer"
        )
        assert change.requires_publication
        assert await load_cached_active_snapshot(session, group) == before
        await publish_rule_version(session, change.version_id, operator="synthetic-reviewer")
        after = await load_cached_active_snapshot(session, group)
        assert change.version_id in after.version_ids
        assert version_id not in after.version_ids
        assert not any(item.pattern == "synthetic-rule-target" for item in after.items)
        assert any(item.pattern == "synthetic-rule-retained" for item in after.items)
    assert (await _items(version_id))[0] == ("synthetic-rule-target", True)


@pytest.mark.asyncio
async def test_restoring_active_disabled_item_also_requires_a_new_version() -> None:
    _version_id, item_id, _rule_set_id, group = await _seed("DRAFT")
    async with SessionLocal() as session:
        disabled = await set_rule_item_enabled(
            session, item_id, enabled=False, operator="synthetic-reviewer"
        )
        await publish_rule_version(session, disabled.version_id, operator="synthetic-reviewer")
        restored = await set_rule_item_enabled(
            session, item_id, enabled=True, operator="synthetic-reviewer"
        )
        assert restored.created_draft and restored.requires_publication
        assert restored.version_id != disabled.version_id
        snapshot = await load_cached_active_snapshot(session, group)
        assert not any(item.pattern == "synthetic-rule-target" for item in snapshot.items)
        await publish_rule_version(session, restored.version_id, operator="synthetic-reviewer")
        snapshot = await load_cached_active_snapshot(session, group)
        assert any(item.pattern == "synthetic-rule-target" for item in snapshot.items)
    assert (await _items(disabled.version_id))[0] == ("synthetic-rule-target", False)


@pytest.mark.asyncio
async def test_cleared_candidate_cannot_be_copied_and_clear_retry_is_noop() -> None:
    async with SessionLocal() as session:
        candidate = RuleCandidate(
            scope_key=f"r109-purged-{uuid.uuid4().hex}",
            item_type="keyword",
            pattern="synthetic-cleared-rule",
            status="DISMISSED",
        )
        session.add(candidate)
        await session.commit()
        candidate_id = candidate.id
        first = await purge_dismissed_rule_candidates(session, operator="synthetic-reviewer")
        assert first["purged"] >= 1
        second = await purge_dismissed_rule_candidates(session, operator="synthetic-reviewer")
        assert second["purged"] == 0
        with pytest.raises(ValueError, match="已清理"):
            await copy_candidate_to_draft(session, candidate_id, operator="synthetic-reviewer")


@pytest.mark.asyncio
async def test_retention_and_remining_never_delete_or_rewrite_a_purged_candidate_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.reports.cleanup import purge_expired

    monkeypatch.setattr("app.runtime.pipeline.MEDIA_DIR", tmp_path)
    group = f"r109-purged-lifecycle-{uuid.uuid4().hex}"
    phrase = "合成候选短语"
    async with SessionLocal() as session:
        candidate = RuleCandidate(
            scope_key=group,
            item_type="keyword",
            pattern=phrase,
            status="DISMISSED",
            created_at=datetime.now(UTC) - timedelta(days=365),
        )
        session.add(candidate)
        await session.commit()
        purged_id = candidate.id
        await purge_dismissed_rule_candidates(session, operator="synthetic-reviewer")
        for index in range(3):
            await record_feedback(
                session,
                f"{group}-message-{index}",
                "confirmed_violation",
                "ad",
                "synthetic-reviewer",
                group_openid=group,
                member_openid=f"synthetic-member-{index}",
                sample_text=phrase,
            )
        await purge_expired(session)
        generated = await mine_rule_candidates(session)
        candidate = await session.get(RuleCandidate, purged_id, populate_existing=True)
        assert candidate is not None
        assert candidate.status == "PURGED"
        assert candidate.pattern == "" and candidate.replay_report_json == "{}"
        # Re-mining the same still-valid feedback is not a permanent rejection
        # blacklist; a new proposal must have its own ID, never revive the old one.
        matching = [
            item for item in generated if item.scope_key == group and item.pattern == phrase
        ]
        assert matching and all(item.id != purged_id for item in matching)


@pytest.mark.parametrize("route", ["disable", "enable", "purge-dismissed"])
def test_rule_management_requires_auth_and_csrf(client: TestClient, route: str) -> None:
    version_id, item_id, _rule_set_id, _group = asyncio.run(_seed())
    before = asyncio.run(_items(version_id))
    url = (
        "/admin/feedback/candidates/purge-dismissed"
        if route == "purge-dismissed"
        else f"/admin/rules/items/{item_id}/{route}"
    )
    assert client.post(url, follow_redirects=False).status_code == 403
    auth.logout(client.cookies[auth.SESSION_COOKIE])
    assert client.post(url, follow_redirects=False).status_code == 401
    assert asyncio.run(_items(version_id)) == before
