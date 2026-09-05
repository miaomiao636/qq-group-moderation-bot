"""T-403 影子流水线测试：解析→去重→判定→记录，绝不执行动作。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from app.db import SessionLocal
from app.runtime.pipeline import run_pipeline

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "qq_official"


def load_payload(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))["data"]


@pytest.mark.asyncio
async def test_spam_fixture_recorded_as_violation() -> None:
    payload = load_payload("group_message_create_text_spam_1.json")
    marker = f"SHADOW_{uuid.uuid4().hex[:8]}"
    payload["id"] = marker
    async with SessionLocal() as session:
        record = await run_pipeline(payload, session)
        assert record is not None
        assert record.verdict in ("violation_high", "record_only")
        assert record.message_id == marker
        detail = json.loads(record.detail_json)
        assert "kick" not in json.dumps(detail).lower()
        assert detail["recommended_actions"] in ([], ["recall", "mute", "warn"])


@pytest.mark.asyncio
async def test_duplicate_payload_skipped() -> None:
    payload = load_payload("group_message_create_text_plain.json")
    marker = f"SHADOW_DUP_{uuid.uuid4().hex[:8]}"
    payload["id"] = marker
    async with SessionLocal() as session:
        assert await run_pipeline(payload, session) is not None
        assert await run_pipeline(payload, session) is None  # 去重拦截


@pytest.mark.asyncio
async def test_normal_text_allowed() -> None:
    payload = load_payload("group_message_create_text_plain.json")
    payload["id"] = f"SHADOW_OK_{uuid.uuid4().hex[:8]}"
    payload["content"] = "今天天气真不错"
    payload["author"] = {**payload["author"], "member_role": "member"}
    async with SessionLocal() as session:
        record = await run_pipeline(payload, session)
        assert record is not None
        assert record.verdict == "allow"


@pytest.mark.asyncio
async def test_owner_message_never_high() -> None:
    payload = load_payload("group_message_create_text_spam_2.json")
    payload["id"] = f"SHADOW_OWNER_{uuid.uuid4().hex[:8]}"
    payload["author"] = {**payload["author"], "member_role": "owner"}
    async with SessionLocal() as session:
        record = await run_pipeline(payload, session)
        assert record is not None
        assert record.verdict != "violation_high"
        detail = json.loads(record.detail_json)
        assert detail["is_protected_sender"] is True or record.verdict == "record_only"


@pytest.mark.asyncio
async def test_missing_id_returns_none() -> None:
    payload = load_payload("group_message_create_text_plain.json")
    payload["id"] = ""
    async with SessionLocal() as session:
        assert await run_pipeline(payload, session) is None


@pytest.mark.asyncio
async def test_decision_page_renders(logged_in_web) -> None:
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-admin-pass"},
            follow_redirects=False,
        )
        resp = client.get("/admin/shadow")
        assert resp.status_code == 200
        assert "影子模式判定" in resp.text


@pytest.fixture()
def logged_in_web():
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.post(
            "/admin/login",
            data={"username": "admin", "password": "test-admin-pass"},
            follow_redirects=False,
        )
        yield client
