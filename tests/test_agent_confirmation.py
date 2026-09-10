"""P0-2: Agent high-risk operation two-phase confirmation tests."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient


def _app_with_agent():
    import os

    os.environ["AGENT_API_TOKEN"] = "test-agent-token-12345"
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import app

    yield app
    os.environ.pop("AGENT_API_TOKEN", None)
    get_settings.cache_clear()


@pytest.fixture
def app():
    yield from _app_with_agent()


_BEARER = {"Authorization": "Bearer test-agent-token-12345"}


def test_action_enable_requires_confirmation(app):
    """Enabling action_enabled returns 202 + confirmation token, not direct execution."""
    with TestClient(app) as client:
        resp = client.post(
            "/admin/api/groups/test-confirm/settings",
            params={"action_enabled": "true"},
            headers=_BEARER,
        )
        assert resp.status_code == 202, f"got {resp.status_code}: {resp.text[:300]}"
        data = resp.json()
        assert data["confirmation_required"] is True
        assert "confirmation_token" in data
        assert "group_action_enable" in data["summary"]


def test_action_enable_with_valid_token_executes(app):
    """Two-phase flow: first get token, then confirm -> executed."""
    with TestClient(app) as client:
        # Phase 1: get confirmation token
        resp1 = client.post(
            "/admin/api/groups/test-confirm-ok/settings",
            params={"action_enabled": "true"},
            headers=_BEARER,
        )
        assert resp1.status_code == 202
        token = resp1.json()["confirmation_token"]

        # Phase 2: confirm and execute
        resp2 = client.post(
            "/admin/api/groups/test-confirm-ok/settings",
            params={"action_enabled": "true", "confirm_token": token},
            headers=_BEARER,
        )
        assert resp2.status_code == 200
        assert resp2.json()["action_enabled"] is True


def test_confirmation_replay_rejected(app):
    """Same token cannot be used twice (replay attack)."""
    with TestClient(app) as client:
        resp1 = client.post(
            "/admin/api/groups/test-replay/settings",
            params={"action_enabled": "true"},
            headers=_BEARER,
        )
        token = resp1.json()["confirmation_token"]

        # First use succeeds
        resp2 = client.post(
            "/admin/api/groups/test-replay/settings",
            params={"action_enabled": "true", "confirm_token": token},
            headers=_BEARER,
        )
        assert resp2.status_code == 200

        # Replay with same token -> 403
        resp3 = client.post(
            "/admin/api/groups/test-replay/settings",
            params={"action_enabled": "true", "confirm_token": token},
            headers=_BEARER,
        )
        assert resp3.status_code == 403


def test_confirmation_wrong_token_rejected(app):
    """Invalid confirmation token -> 403."""
    with TestClient(app) as client:
        resp = client.post(
            "/admin/api/groups/test-wrong/settings",
            params={"action_enabled": "true", "confirm_token": "invalid-token"},
            headers=_BEARER,
        )
        assert resp.status_code == 403


def test_confirmation_bound_to_group(app):
    """Token for group A cannot confirm operation for group B (summary binding)."""
    with TestClient(app) as client:
        # Get token for group A
        resp1 = client.post(
            "/admin/api/groups/group-A-confirm/settings",
            params={"action_enabled": "true"},
            headers=_BEARER,
        )
        token_a = resp1.json()["confirmation_token"]

        # Try to use it for group B -> 403 (summary mismatch)
        resp2 = client.post(
            "/admin/api/groups/group-B-other/settings",
            params={"action_enabled": "true", "confirm_token": token_a},
            headers=_BEARER,
        )
        assert resp2.status_code == 403


def test_moderation_enabled_no_confirmation_needed(app):
    """Low-risk operation (moderation_enabled) does not need confirmation."""
    with TestClient(app) as client:
        resp = client.post(
            "/admin/api/groups/test-lowrisk/settings",
            params={"moderation_enabled": "true"},
            headers=_BEARER,
        )
        assert resp.status_code == 200
        assert resp.json()["moderation_enabled"] is True


def test_action_disable_no_confirmation_needed(app):
    """Disabling action_enabled (safer direction) does not need confirmation."""
    with TestClient(app) as client:
        resp = client.post(
            "/admin/api/groups/test-disable/settings",
            params={"action_enabled": "false"},
            headers=_BEARER,
        )
        assert resp.status_code == 200
        assert resp.json()["action_enabled"] is False
