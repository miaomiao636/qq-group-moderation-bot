# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-8 probe pack (8299ce8), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Synthetic service/persistence and configuration controls; no external calls."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.moderation import image_hash
from app.moderation.ai import AIReviewService
from app.runtime import pipeline
from tests.test_r132_image_hash import _image_bytes, clean_allowlist
from tests.test_r132_review_offline_configured_review import FixedVision
from tests.test_r132_review_online_offline_pair_consistency import seed
from tests.test_r132_window_evidence import event, execute, ident


@pytest.mark.parametrize("direct,pconf", [(0.80, 0.85), (0.95, 0.97), (0.90, 0.97)])
async def test_snapshot_preserves_actual_nondefault_direct_threshold(
    monkeypatch, tmp_path, clean_allowlist, direct, pconf
):
    monkeypatch.setenv("IMAGE_HASH_MODE", "shadow")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    image = _image_bytes(0)
    name = f"{ident()}.png"
    (tmp_path / name).write_bytes(image)
    await seed(image)
    group = str(ident())
    primary, secondary = FixedVision(pconf), FixedVision(0.99)
    service = AIReviewService(
        enabled=True,
        enabled_groups={group},
        vision_moderator=primary,
        review_vision_moderator=secondary,
        primary_direct_threshold=direct,
    )
    row = await execute(
        event(
            group, ident(), datetime.now(UTC), [{"type": "image", "data": {"file": name}}], (name,)
        ),
        service,
    )
    assert row is not None and row.verdict == "violation_high"
    assert primary.calls == 1 and secondary.calls == 0
    detail = json.loads(row.detail_json)
    assert detail["review_policy"]["primary_direct_threshold"] == direct, detail
    # This test checks the persisted policy, not other existing hash blockers.


@pytest.mark.parametrize(
    "env,configured,expected",
    [
        (None, "shadow", "shadow"),
        ("off", "shadow", "off"),
        ("shadow", "off", "shadow"),
        ("invalid", "shadow", "off"),
        (None, "invalid", "off"),
        (None, "off", "off"),
    ],
)
def test_mode_configuration_precedence(monkeypatch, env, configured, expected):
    if env is None:
        monkeypatch.delenv("IMAGE_HASH_MODE", raising=False)
    else:
        monkeypatch.setenv("IMAGE_HASH_MODE", env)
    monkeypatch.setattr(
        "app.config.get_settings", lambda: SimpleNamespace(image_hash_mode=configured)
    )
    assert image_hash.mode() == expected


def test_mode_fails_closed_when_application_config_unreadable(monkeypatch):
    monkeypatch.delenv("IMAGE_HASH_MODE", raising=False)

    def unavailable():
        raise OSError("synthetic configuration read failure")

    monkeypatch.setattr("app.config.get_settings", unavailable)
    assert image_hash.mode() == "off"
