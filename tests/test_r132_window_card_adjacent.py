# ruff: noqa: E402, I001, F401
# Reviewer round-2 probe pack (a354d17), promoted verbatim into the repo test suite.
# Isolation asserts intentionally run BEFORE application imports (E402 is by design).
# This header changes no assertion and no logic.
import json
import socket
from datetime import UTC, datetime, timedelta

import pytest

# Import guards require conftest preload and a fresh SHADOW-only temporary DB.
from tests.test_r132_window_evidence import (
    AIModerationResult,
    SyntheticModels,
    execute,
    event,
    ident,
    source_image,
)
from tests.test_r132_structure_probe import GROUP, json_segment
from app.adapters.onebot.parser import OneBotMessageSource
from app.db import SessionLocal
from app.moderation.allowlist import add_member
from app.runtime import pipeline
from app.runtime.inbox import enqueue_event


@pytest.fixture(autouse=True)
def network_forbidden(monkeypatch):
    # Windows 适配（非断言变更）：Windows 的 ProactorEventLoop 在自管道上使用
    # socketpair 会触发本机回环 connect，原守卫会把它误判为"意外网络连接"。
    # 这里仍然**禁止一切非本机连接**，仅放行 loopback —— 探针意图不变。
    real_connect = socket.socket.connect

    def denied(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost", ""):
            return real_connect(self, address, *args, **kwargs)
        pytest.fail("Unexpected network connection in isolated SHADOW probe")

    monkeypatch.setattr(socket.socket, "connect", denied)


def snapshot(row):
    detail = json.loads(row.detail_json)
    return {
        "kind": row.kind,
        "verdict": row.verdict,
        "category": row.category,
        "reason": row.reason,
        "hits": [h["rule_id"] for h in detail["rule_hits"]],
        "actions": detail["recommended_actions"],
    }


@pytest.mark.parametrize("prior_source", [False, True], ids=["no-window", "inside-window"])
@pytest.mark.parametrize("reverse", [False, True], ids=["image-first", "card-first"])
async def test_image_group_card_always_retains_structural_recall(
    monkeypatch, tmp_path, prior_source, reverse
):
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    if prior_source:
        await source_image(group, user, now - timedelta(seconds=10))
    (tmp_path / "a.png").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    segments = [{"type": "image", "data": {"file": "a.png"}}, json_segment(GROUP)]
    if reverse:
        segments.reverse()
    raw = event(group, user, now, segments, ("a.png",))
    row = await execute(raw, SyntheticModels())
    got = snapshot(row)
    print("image_group", prior_source, reverse, got)
    assert "R_GROUP_CARD" in got["hits"]
    assert got["verdict"] == "violation_high", got
    assert "recall" in got["actions"], got


@pytest.mark.parametrize(
    "app", ["com.example.groupbuy", "com.example.quniversity", "com.tencent.news"]
)
async def test_app_substring_alone_does_not_make_structural_group_card(app):
    raw = event(
        ident(),
        ident(),
        datetime.now(UTC),
        [
            json_segment(
                {
                    "app": app,
                    "view": "news",
                    "title": "本周电影资讯",
                    "prompt": "新片观影指南",
                    "meta": {"news": {"title": "本周电影资讯"}},
                }
            )
        ],
    )
    row = await execute(raw, SyntheticModels())
    got = snapshot(row)
    print("app_substring", app, got)
    assert "R_GROUP_CARD" not in got["hits"], got
    assert got["verdict"] != "violation_high", got


@pytest.mark.parametrize("group_position", [0, 1, 2])
async def test_multiple_cards_keep_any_group_card(group_position):
    normal = json_segment({"app": "com.tencent.news", "title": "电影资讯"})
    segments = [normal, normal]
    segments.insert(group_position, json_segment(GROUP))
    raw = event(ident(), ident(), datetime.now(UTC), segments)
    row = await execute(raw, SyntheticModels())
    got = snapshot(row)
    assert got["verdict"] == "violation_high", got
    assert "R_GROUP_CARD" in got["hits"], got


@pytest.mark.parametrize("role", ["owner", "admin", "allowlisted"])
async def test_image_group_card_protected_identity_stays_allow(monkeypatch, tmp_path, role):
    group, user = ident(), ident()
    (tmp_path / "a.png").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    raw = event(
        group,
        user,
        datetime.now(UTC),
        [
            {"type": "image", "data": {"file": "a.png"}},
            json_segment(GROUP),
        ],
        ("a.png",),
    )
    if role == "allowlisted":
        async with SessionLocal() as session:
            await add_member(session, str(user), operator="test:independent")
    else:
        raw["sender"]["role"] = role
    row = await execute(raw, SyntheticModels())
    got = snapshot(row)
    assert got["verdict"] == "allow", got
    assert got["actions"] == [], got


@pytest.mark.parametrize("prefix", [False, True], ids=["bare", "ordinary-text"])
async def test_allowed_campus_card_remains_allowed_with_ordinary_text(prefix):
    segments = [
        json_segment({"app": "com.tencent.miniapp", "source": "万能校园墙", "title": "校园活动"})
    ]
    if prefix:
        segments.insert(0, {"type": "text", "data": {"text": "看看"}})
    raw = event(ident(), ident(), datetime.now(UTC), segments)
    row = await execute(raw, SyntheticModels())
    got = snapshot(row)
    print("allowed_card", prefix, got)
    assert got["verdict"] == "allow", got


async def make_source(group, user, when, evidence, monkeypatch, tmp_path, qr=False):
    (tmp_path / "a.png").touch()
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    raw = event(group, user, when, [{"type": "image", "data": {"file": "a.png"}}], ("a.png",))
    row = await execute(
        raw,
        SyntheticModels(
            [
                AIModerationResult(
                    source="vision",
                    category=None,
                    confidence=1,
                    needs_review=False,
                    evidence=evidence,
                    has_miniprogram_code=qr,
                    model_id="synthetic-source",
                )
            ]
        ),
    )
    assert row.verdict in ("allow", "record_only"), snapshot(row)
    return row


@pytest.mark.parametrize(
    "campus_source", [False, True], ids=["pending-only", "campus-plus-pending"]
)
async def test_same_second_pending_image_protects_fraud_with_prior_campus_source(
    monkeypatch, tmp_path, campus_source
):
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    if campus_source:
        await make_source(
            group,
            user,
            now - timedelta(seconds=10),
            "校园墙白名单|文案:校园活动",
            monkeypatch,
            tmp_path,
        )
    pending = event(group, user, now, [{"type": "image", "data": {"file": "pending.png"}}])
    async with SessionLocal() as session:
        await enqueue_event(session, pending, max_pending=100000)
    raw = event(group, user, now, [{"type": "text", "data": {"text": "合成普通文字"}}])
    row = await execute(
        raw,
        SyntheticModels(
            [
                AIModerationResult(
                    source="text",
                    category="fraud",
                    confidence=0.99,
                    needs_review=False,
                    evidence="synthetic fraud",
                    model_id="synthetic-current",
                )
            ]
        ),
    )
    got = snapshot(row)
    print("campus_pending", campus_source, got)
    assert got["verdict"] == "record_only", got
    assert got["actions"] == [], got
    if not campus_source:
        assert "未完成" in got["reason"], got


@pytest.mark.parametrize("campus_first", [False, True], ids=["qr-first", "campus-first"])
async def test_source_row_order_does_not_hide_eligible_qr_source(
    monkeypatch, tmp_path, campus_first
):
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    sources = [("校园墙白名单|文案:校园活动", False), ("小程序码通过|文案:合成活动", True)]
    if not campus_first:
        sources.reverse()
    for index, (evidence, qr) in enumerate(sources):
        await make_source(
            group, user, now - timedelta(seconds=20 - index), evidence, monkeypatch, tmp_path, qr
        )
    raw = event(group, user, now, [{"type": "text", "data": {"text": "合成普通文字"}}])
    row = await execute(
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
    got = snapshot(row)
    print("source_order", campus_first, got)
    assert got["verdict"] == "record_only", got
    assert got["actions"] == [], got


@pytest.mark.parametrize(
    "qr", [False, True], ids=["prefix-only-flag-false", "structured-flag-true"]
)
async def test_fraud_source_prefix_requires_matching_qr_flag(monkeypatch, tmp_path, qr):
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    await make_source(
        group,
        user,
        now - timedelta(seconds=10),
        "小程序码通过|文案:合成活动",
        monkeypatch,
        tmp_path,
        qr,
    )
    raw = event(group, user, now, [{"type": "text", "data": {"text": "合成普通文字"}}])
    row = await execute(
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
    got = snapshot(row)
    print("qr_flag", qr, got)
    assert got["verdict"] == ("record_only" if qr else "violation_high"), got
