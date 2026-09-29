# ruff: noqa: E402, I001, F401, F811, ASYNC109, B905
# Reviewer round-5 probe pack (20cccd5), promoted verbatim except the Q01 contract change
# documented in docs/2026-09-18-r132-round5-remediation.md.
"""Read-only review controls for 20cccd5, real AIReviewService -> pipeline -> DB.

Run from the reviewed worktree with PYTHONPATH=.:tests, -p conftest and -c pyproject.toml.
All provider results are synthetic; no OneBot/model network operation is permitted.
"""

import ipaddress
import json
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

# This module verifies SHADOW/temp-DB isolation before importing application code.
from tests.test_r132_window_evidence import AIModerationResult, execute, event, ident
from app.db import SessionLocal
from app.moderation.ai import (
    AICacheEntry,
    AIProviderError,
    AIQuota,
    AIReviewService,
    AIUsageLog,
    MAX_AI_MEDIA_BYTES,
)
from app.moderation import wall_pair
from app.runtime import pipeline
from app.runtime.models import ShadowDecision


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect

    def deny(sock, address):
        # Only Windows' Proactor socketpair may use literal loopback IPs.
        # All non-Windows platforms remain deny-all; no localhost hostname exception.
        if sys.platform == "win32" and isinstance(address, tuple):
            try:
                if ipaddress.ip_address(address[0]).is_loopback:
                    return real_connect(sock, address)
            except ValueError:
                pass
        pytest.fail("Unexpected socket connection in isolated R09 review")

    monkeypatch.setattr(socket.socket, "connect", deny)


async def persisted(row):
    assert row is not None
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(ShadowDecision).where(ShadowDecision.message_id == row.message_id)
            )
        ).scalar_one()


def detail(row):
    return json.loads(row.detail_json)


def observed(row):
    data = detail(row)
    return {
        "kind": row.kind,
        "verdict": row.verdict,
        "reason": row.reason,
        "actions": data["recommended_actions"],
        "evidence_vetoes": data.get("evidence_vetoes"),
        "ai_results": [
            {
                key: result[key]
                for key in (
                    "source",
                    "category",
                    "needs_review",
                    "degraded_reason",
                    "cache_hit",
                    "has_miniprogram_code",
                    "evidence",
                    "review_role",
                )
            }
            for result in data["ai_results"]
        ],
    }


class Vision:
    def __init__(self, second="normal"):
        self.model_id = "synthetic-r09-vision-" + uuid.uuid4().hex
        self.prompt_version = "review-r09-real-shapes"
        self.second = second
        self.calls = []

    async def moderate_image(self, request):
        self.calls.append(request.media_bytes)
        qr = request.media_bytes == b"synthetic-qr"
        if not qr and self.second == "timeout":
            raise AIProviderError("synthetic_provider_timeout")
        return AIModerationResult(
            model_id=self.model_id,
            category=None,
            confidence=1,
            needs_review=(not qr and self.second == "needs_review"),
            has_miniprogram_code=qr,
            evidence="小程序码通过|文案:合成活动" if qr else "合成普通图",
        )


class Text:
    def __init__(self, mode="normal"):
        self.model_id = "synthetic-r09-text-" + uuid.uuid4().hex
        self.prompt_version = "review-r09-real-shapes"
        self.mode = mode
        self.calls = []

    async def moderate_text(self, request):
        self.calls.append(request.text)
        if self.mode == "timeout":
            raise AIProviderError("synthetic_text_timeout")
        return AIModerationResult(
            model_id=self.model_id,
            category="fraud" if self.mode == "fraud" else None,
            confidence=0.99,
            needs_review=self.mode == "needs_review",
            evidence="合成文字证据",
        )


def service(group, vision=None, text=None, **kwargs):
    return AIReviewService(
        enabled=True,
        enabled_groups={str(group)},
        vision_moderator=vision,
        text_moderator=text,
        **kwargs,
    )


def source_payload(group, user, when, filenames, extra=()):
    return event(
        group,
        user,
        when,
        [
            *extra,
            *({"type": "image", "data": {"file": name}} for name in filenames),
        ],
        filenames,
    )


async def next_fraud(group, user, when):
    row = await persisted(
        await execute(
            event(
                group,
                user,
                when,
                [
                    {"type": "text", "data": {"text": "独立合成后续消息"}},
                ],
            ),
            service(group, text=Text("fraud")),
        )
    )
    assert any(
        result["source"] == "text" and result["category"] == "fraud"
        for result in detail(row)["ai_results"]
    ), observed(row)
    return row


async def assert_source_boundary(source, group, user, now, *, eligible):
    assert source.verdict in ("allow", "record_only"), observed(source)
    assert detail(source)["recommended_actions"] == [], observed(source)
    current = await next_fraud(group, user, now)
    print("SOURCE", observed(source))
    print("CURRENT", observed(current))
    if eligible:
        assert wall_pair._confirmed_sources(detail(source)), observed(source)
        assert current.verdict == "record_only", observed(current)
        assert detail(current)["recommended_actions"] == [], observed(current)
    else:
        assert current.verdict == "violation_high", observed(current)
        assert "recall" in detail(current)["recommended_actions"], observed(current)
        assert wall_pair._confirmed_sources(detail(source)) == [], observed(source)


@pytest.mark.parametrize("second", ["normal", "timeout", "needs_review", "rate_limit"])
async def test_cache_is_real_persisted_vision_not_cache_source(monkeypatch, tmp_path, second):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    (tmp_path / "qr.png").write_bytes(b"synthetic-qr")
    (tmp_path / "other.png").write_bytes(b"synthetic-other")
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    model = Vision(second)
    # Warm via the actual service and pipeline, outside the exemption window.
    # For timeout use only QR, so the target restores QR and generates a fresh failure.
    warm_files = ("qr.png",) if second in ("timeout", "rate_limit") else ("qr.png", "other.png")
    await persisted(
        await execute(
            source_payload(group, user, now - timedelta(seconds=300), warm_files),
            service(group, vision=model),
        )
    )
    calls_after_warm = len(model.calls)
    async with SessionLocal() as session:
        cache = (
            await session.scalars(
                select(AICacheEntry).where(AICacheEntry.model_id == model.model_id)
            )
        ).all()
        assert len(cache) == len(warm_files)
        assert all(json.loads(row.result_json)["source"] == "vision" for row in cache)
        assert all(json.loads(row.result_json)["cache_hit"] is False for row in cache)
    # A fresh service and fresh DB session must restore cache without a provider call.
    restored_service = service(group, vision=model)
    if second == "rate_limit":
        restored_service.quota = AIQuota(per_minute_limit=0)
    source = await persisted(
        await execute(
            source_payload(group, user, now - timedelta(seconds=10), ("qr.png", "other.png")),
            restored_service,
        )
    )
    assert len(model.calls) == calls_after_warm + (second == "timeout")
    results = detail(source)["ai_results"]
    assert results[0]["source"] == "vision" and results[0]["cache_hit"] is True
    assert not any(result["source"] == "cache" for result in results)
    async with SessionLocal() as session:
        usage = (
            await session.scalars(
                select(AIUsageLog).where(AIUsageLog.message_id == source.external_message_id)
            )
        ).all()
        assert any(row.source == "cache_primary" for row in usage)
    if second != "normal":
        assert source.verdict == "record_only", observed(source)
        assert any(result["source"] == "degraded" for result in results), observed(source)
    if second == "rate_limit":
        assert any(
            result["degraded_reason"] == "ai_rate_or_budget_limited" for result in results
        ), observed(source)
    await assert_source_boundary(source, group, user, now, eligible=second == "normal")


@pytest.mark.parametrize("text_mode", ["normal", "timeout", "needs_review"])
async def test_real_text_channel_on_image_reply(monkeypatch, tmp_path, text_mode):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    (tmp_path / "qr.png").write_bytes(b"synthetic-qr")
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    text = Text(text_mode)
    raw = source_payload(
        group,
        user,
        now - timedelta(seconds=10),
        ("qr.png",),
        extra=({"type": "reply", "data": {"id": "1"}},),
    )
    source = await persisted(await execute(raw, service(group, vision=Vision(), text=text)))
    assert source.kind == "image", observed(source)
    assert text.calls == ["[回复消息:1]"]
    results = detail(source)["ai_results"]
    assert len(results) == 2, observed(source)
    assert results[0]["source"] == ("degraded" if text_mode == "timeout" else "text")
    if text_mode == "timeout":
        assert source.verdict == "record_only", observed(source)
    else:
        # Documented F02 scope is unresolved ATTACHMENTS, not text-channel review.
        # The original remediation doc and ai.py:951-958 explicitly preserve QR
        # allowance for accompanying text. Do not invent a stronger review policy.
        assert source.verdict == "allow", observed(source)
    await assert_source_boundary(source, group, user, now, eligible=text_mode != "timeout")


@pytest.mark.parametrize("failure", ["unreadable", "oversize", "missing-file", "download-failed"])
async def test_actual_missing_or_unreadable_attachment(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    (tmp_path / "qr.png").write_bytes(b"synthetic-qr")
    other = tmp_path / "other.png"
    if failure == "oversize":
        with other.open("wb") as stream:
            stream.truncate(MAX_AI_MEDIA_BYTES + 1)
    elif failure == "unreadable":
        other.write_bytes(b"synthetic-unreadable")
        read_bytes = Path.read_bytes

        def synthetic_permission_error(path):
            if path == other:
                raise PermissionError("synthetic isolated unreadable attachment")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", synthetic_permission_error)
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    files = ("qr.png", "" if failure == "download-failed" else "other.png")
    model = Vision()
    source = await persisted(
        await execute(
            source_payload(group, user, now - timedelta(seconds=10), files),
            service(group, vision=model),
        )
    )
    assert source.verdict == "record_only", observed(source)
    assert model.calls == [b"synthetic-qr"]
    results = detail(source)["ai_results"]
    if failure in ("missing-file", "download-failed"):
        assert len(results) == 1 and results[0]["source"] == "vision", observed(source)
        assert "媒体缺失/下载失败" in detail(source)["evidence_vetoes"], observed(source)
    else:
        reason = "media_unreadable_for_ai" if failure == "unreadable" else "media_too_large_for_ai"
        assert any(
            result["source"] == "degraded" and result["degraded_reason"] == reason
            for result in results
        ), observed(source)
    await assert_source_boundary(source, group, user, now, eligible=False)


@pytest.mark.parametrize("unavailable", ["ai-disabled", "vision-not-configured", "config-problem"])
async def test_absent_ai_result_never_creates_source(monkeypatch, tmp_path, unavailable):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    (tmp_path / "qr.png").write_bytes(b"synthetic-qr")
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    model = Vision()
    reviewer = service(
        group,
        vision=None if unavailable == "vision-not-configured" else model,
        config_problem="synthetic_config_problem" if unavailable == "config-problem" else "",
    )
    if unavailable == "ai-disabled":
        reviewer.enabled = False
    source = await persisted(
        await execute(
            source_payload(group, user, now - timedelta(seconds=10), ("qr.png",)), reviewer
        )
    )
    assert model.calls == []
    results = detail(source)["ai_results"]
    if unavailable == "config-problem":
        assert len(results) == 1 and results[0]["source"] == "degraded"
    else:
        assert results == []
    await assert_source_boundary(source, group, user, now, eligible=False)


async def test_unknown_segment_veto_survives_confirmed_qr(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    (tmp_path / "qr.png").write_bytes(b"synthetic-qr")
    group, user = ident(), ident()
    now = datetime.now(UTC).replace(microsecond=0)
    source = await persisted(
        await execute(
            source_payload(
                group,
                user,
                now - timedelta(seconds=10),
                ("qr.png",),
                extra=({"type": "synthetic_unknown", "data": {}},),
            ),
            service(group, vision=Vision()),
        )
    )
    assert source.kind == "image" and source.verdict == "record_only", observed(source)
    assert "包含无法解析的内容（未知消息段）" in detail(source)["evidence_vetoes"], observed(source)
    assert detail(source)["ai_results"][0]["source"] == "vision"
    await assert_source_boundary(source, group, user, now, eligible=False)
