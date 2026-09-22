# ruff: noqa: I001
# Reviewer round-3 probe pack (7ec5553), promoted verbatim into the repo test suite.
# This header changes no assertion and no logic.
import ipaddress
import json
import socket
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

# Reuse the repository helper's pre-import APP_ENV / SHADOW / temporary-DB guards.
from tests.test_r132_window_evidence import (
    AIModerationResult,
    SyntheticModels,
    execute,
    event,
    ident,
)
from tests.test_r132_structure_probe import json_segment
from app.db import SessionLocal
from app.runtime import pipeline
from app.runtime.models import ShadowDecision


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect

    def deny(sock, address):
        # Windows ProactorEventLoop creates its socketpair on loopback.
        # Keep the original deny-all guard on other platforms; no external host
        # is allowed on Windows, either. Business assertions are unchanged.
        if sys.platform == "win32" and isinstance(address, tuple):
            try:
                if ipaddress.ip_address(address[0]).is_loopback:
                    return real_connect(sock, address)
            except ValueError:
                pass
        pytest.fail("Unexpected external connect")

    monkeypatch.setattr(socket.socket, "connect", deny)


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


async def reload_persisted(row):
    assert row is not None
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(ShadowDecision).where(ShadowDecision.message_id == row.message_id)
            )
        ).scalar_one()


async def full_pair(monkeypatch, tmp_path, results):
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    for filename in ("a.png", "b.png"):
        (tmp_path / filename).touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    # Reverse the actual attachment order together with its bound vision results.
    files = tuple("a.png" if item.review_group == "image-a" else "b.png" for item in results)
    source = event(
        group,
        user,
        now - timedelta(seconds=10),
        [{"type": "image", "data": {"file": filename}} for filename in files],
        files,
    )
    source_row = await reload_persisted(await execute(source, SyntheticModels(results)))
    assert source_row.verdict == "allow", observed(source_row)
    current = event(group, user, now, [{"type": "text", "data": {"text": "合成普通文字"}}])
    current_row = await reload_persisted(
        await execute(
            current,
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
    print("SOURCE", observed(source_row))
    print("CURRENT", observed(current_row))
    return current_row


def result(evidence, qr, group):
    return AIModerationResult(
        source="vision",
        category=None,
        confidence=1.0,
        needs_review=False,
        evidence=evidence,
        has_miniprogram_code=qr,
        review_group=group,
        model_id="synthetic-source",
    )


@pytest.mark.parametrize("qr_first", [False, True], ids=["campus-first", "qr-first"])
async def test_qr_source_within_message_is_order_independent(monkeypatch, tmp_path, qr_first):
    campus = result("校园墙白名单|文案:合成活动甲", False, "image-a")
    qr = result("小程序码通过|文案:合成活动乙", True, "image-b")
    row = await full_pair(monkeypatch, tmp_path, [qr, campus] if qr_first else [campus, qr])
    assert row.verdict == "record_only", observed(row)
    assert json.loads(row.detail_json)["recommended_actions"] == [], observed(row)


@pytest.mark.parametrize("prefix_first", [False, True], ids=["flag-first", "prefix-first"])
async def test_prefix_and_flag_cannot_come_from_different_results(
    monkeypatch, tmp_path, prefix_first
):
    prefix_only = result("小程序码通过|文案:合成活动甲", False, "image-a")
    flag_only = result("校园墙白名单|文案:合成活动乙", True, "image-b")
    row = await full_pair(
        monkeypatch,
        tmp_path,
        [prefix_only, flag_only] if prefix_first else [flag_only, prefix_only],
    )
    assert row.verdict == "violation_high", observed(row)
    assert "recall" in json.loads(row.detail_json)["recommended_actions"], observed(row)


@pytest.mark.parametrize(
    "card, expected_group",
    [
        ({"app": "com.tencent.qun.share", "view": "news", "title": "电影资讯"}, False),
        ({"app": "com.example.news", "view": "group", "title": "电影资讯"}, False),
        ({"app": "com.tencent.qun.share", "view": "group", "title": "研究交流"}, True),
        (
            {"app": "com.tencent.news", "view": "news", "meta": {"group": {}}, "title": "电影资讯"},
            False,
        ),
    ],
)
async def test_exact_group_card_identity_combination(card, expected_group):
    row = await execute(
        event(ident(), ident(), datetime.now(UTC), [json_segment(card)]), SyntheticModels()
    )
    hits = {hit["rule_id"] for hit in json.loads(row.detail_json)["rule_hits"]}
    assert ("R_GROUP_CARD" in hits) is expected_group, observed(row)
