# CAMPUS-SCOPE-20260922: synthetic source fixtures adapted; see docs/2026-09-22-campus-source-scope.md.
# ruff: noqa: E402, I001, F401, F811, ASYNC109
# Reviewer round-4 probe pack (97d68d1), promoted verbatim into the repo test suite.
# This header changes no assertion and no logic.
import ipaddress
import json
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

# These helpers fail before application imports unless conftest created an isolated SHADOW DB.
from tests.test_r132_window_evidence import (
    AIModerationResult,
    SyntheticModels,
    execute,
    event,
    ident,
)
from app.db import SessionLocal
from app.moderation.ai import AIProviderError, AIReviewService
from app.moderation import wall_pair
from app.runtime import pipeline
from app.runtime.models import ShadowDecision


from tests.campus_fixtures import campus_evidence


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect

    def deny(sock, address):
        # Only Windows' Proactor socketpair may connect to a literal loopback IP.
        # Every other platform keeps deny-all; localhost names are never allowed.
        if sys.platform == "win32" and isinstance(address, tuple):
            try:
                if ipaddress.ip_address(address[0]).is_loopback:
                    return real_connect(sock, address)
            except ValueError:
                pass
        pytest.fail("Unexpected socket connection in isolated review")

    monkeypatch.setattr(socket.socket, "connect", deny)


async def reload_persisted(row):
    assert row is not None
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(ShadowDecision).where(ShadowDecision.message_id == row.message_id)
            )
        ).scalar_one()


def observed(row):
    detail = json.loads(row.detail_json)
    return {
        "verdict": row.verdict,
        "reason": row.reason,
        "category": row.category,
        "actions": detail["recommended_actions"],
        "ai_results": detail["ai_results"],
        "hits": [hit["rule_id"] for hit in detail["rule_hits"]],
        "action_intents": detail.get("action_intents", []),
    }


def vision(mark="校园墙白名单", qr=False, group="image-a"):
    return AIModerationResult(
        source="vision",
        category=None,
        confidence=1,
        needs_review=False,
        has_miniprogram_code=qr,
        campus_wall_source="万能校园墙",
        evidence=campus_evidence(f"{mark}|文案:合成活动"),
        model_id="synthetic-source",
        review_group=group,
    )


async def source_event(group, user, when, results, tmp_path):
    files = tuple(f"{index}.png" for index in range(len(results)))
    for filename in files:
        (tmp_path / filename).touch()
    raw = event(
        group,
        user,
        when,
        [{"type": "image", "data": {"file": filename}} for filename in files],
        files,
    )
    row = await reload_persisted(await execute(raw, SyntheticModels(results)))
    assert row.verdict in ("allow", "record_only"), observed(row)
    if any(item.has_miniprogram_code for item in results):
        assert row.verdict == "allow", observed(row)
    return row


async def fraud_event(group, user, when):
    raw = event(group, user, when, [{"type": "text", "data": {"text": "合成普通文字"}}])
    return await reload_persisted(
        await execute(
            raw,
            SyntheticModels(
                [
                    AIModerationResult(
                        source="text",
                        category="fraud",
                        confidence=0.99,
                        needs_review=False,
                        model_id="synthetic-current",
                    )
                ]
            ),
        )
    )


@pytest.mark.parametrize("qr_message_first", [False, True])
@pytest.mark.parametrize("qr_image_first", [False, True])
async def test_cross_message_and_within_message_order_independent(
    monkeypatch, tmp_path, qr_message_first, qr_image_first
):
    def legacy_view_forbidden(*args, **kwargs):
        pytest.fail("Production used the first-source compatibility view")

    monkeypatch.setattr(wall_pair, "_confirmed_source", legacy_view_forbidden)
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    qr_results = [vision(), vision("小程序码通过", True, "image-b")]
    if qr_image_first:
        qr_results.reverse()
    rows = [qr_results, [vision(group="image-c"), vision(group="image-d")]]
    if not qr_message_first:
        rows.reverse()
    for offset, results in enumerate(rows):
        await source_event(group, user, now - timedelta(seconds=20 - offset), results, tmp_path)
    row = await fraud_event(group, user, now)
    assert row.verdict == "record_only", observed(row)
    assert json.loads(row.detail_json)["recommended_actions"] == [], observed(row)


@pytest.mark.parametrize(
    "bad",
    [
        {"category": "porn"},
        {"needs_review": True},
        {"degraded_reason": "synthetic_timeout"},
        {"needs_review": 1},
        {"degraded_reason": None},
        {"category": ""},
    ],
)
@pytest.mark.parametrize("bad_first", [False, True])
def test_r09_any_unconfirmed_vision_vetoes_entire_source(bad, bad_first):
    good = vision("小程序码通过", True).model_dump()
    invalid = vision(group="image-b").model_dump() | bad
    results = [invalid, good] if bad_first else [good, invalid]
    assert wall_pair._confirmed_sources({"ai_results": results}) == []


@pytest.mark.parametrize("flag", [False, 0, 1, "true", None, [], {}, True])
def test_qr_source_requires_exact_boolean(flag):
    item = vision("小程序码通过", True).model_dump()
    item["has_miniprogram_code"] = flag
    sources = wall_pair._confirmed_sources({"ai_results": [item]})
    assert len(sources) == 1
    assert sources[0][2] is (flag is True)


@pytest.mark.parametrize(
    "evidence",
    [
        "不符合小程序码通过|文案:合成活动",
        "转述小程序码通过|文案:合成活动",
        "小程序码通过的说明|文案:合成活动",
        "小程序码通过|文案:",
    ],
)
def test_qr_prefix_and_nonempty_text_remain_strict(evidence):
    item = vision("小程序码通过", True).model_dump() | {"evidence": evidence}
    assert wall_pair._confirmed_sources({"ai_results": [item]}) == []


@pytest.mark.parametrize("flags", [{"processing": True}, {"evidence_vetoes": ["missing media"]}])
def test_processing_or_pipeline_veto_prevents_source(flags):
    detail = {"ai_results": [vision("小程序码通过", True).model_dump()]} | flags
    assert wall_pair._confirmed_sources(detail) == []


class FixedVision:
    def __init__(self, qr, timeout):
        self.qr = qr
        self.timeout = timeout
        self.model_id = "synthetic-" + uuid.uuid4().hex
        self.prompt_version = "synthetic-source-r09"

    async def moderate_image(self, request):
        is_second = request.media_bytes == b"synthetic-second"
        if is_second and self.timeout:
            raise AIProviderError("synthetic_timeout")
        qr = self.qr and not is_second
        return AIModerationResult(
            model_id=self.model_id,
            source="vision",
            category=None,
            confidence=1,
            needs_review=False,
            has_miniprogram_code=qr,
            campus_wall_source="万能校园墙",
            evidence=campus_evidence("小程序码通过|文案:合成活动" if qr else "合成普通图"),
        )


@pytest.mark.parametrize("qr", [False, True], ids=["no-qr-control", "qr-first-image"])
@pytest.mark.parametrize("timeout", [False, True], ids=["all-reviewed", "second-image-timeout"])
async def test_degraded_attachment_cannot_create_confirmed_window_source(
    monkeypatch, tmp_path, qr, timeout
):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    (tmp_path / "first.png").write_bytes(b"synthetic-first")
    (tmp_path / "second.png").write_bytes(b"synthetic-second")
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    raw = event(
        group,
        user,
        now - timedelta(seconds=10),
        [
            {"type": "image", "data": {"file": "first.png"}},
            {"type": "image", "data": {"file": "second.png"}},
        ],
        ("first.png", "second.png"),
    )
    service = AIReviewService(
        enabled=True, enabled_groups={str(group)}, vision_moderator=FixedVision(qr, timeout)
    )
    source = await reload_persisted(await execute(raw, service))
    source_detail = json.loads(source.detail_json)
    if timeout:
        assert source.verdict == "record_only", observed(source)
        assert any(
            item["source"] == "degraded" and item["degraded_reason"] == "synthetic_timeout"
            for item in source_detail["ai_results"]
        ), observed(source)
    elif qr:
        assert source.verdict == "allow", observed(source)
    current = await fraud_event(group, user, now)
    print("SOURCE", observed(source))
    print("CURRENT", observed(current))
    expected = "record_only" if qr and not timeout else "violation_high"
    assert current.verdict == expected, observed(current)
