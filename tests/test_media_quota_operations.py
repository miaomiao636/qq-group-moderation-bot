"""Internal regression for media capacity; synthetic files/network only."""

import json
from pathlib import Path

import httpx
import pytest
from app.adapters.qq_official import media
from app.config import Settings, get_settings
from app.runtime import onebot_wiring, runner


def test_media_quota_is_configurable_and_positive(monkeypatch):
    monkeypatch.setenv("MEDIA_QUOTA_BYTES", str(20 * 1024**3))
    assert Settings(_env_file=None).media_quota_bytes == 20 * 1024**3
    monkeypatch.setenv("MEDIA_QUOTA_BYTES", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


async def test_download_uses_configured_quota_and_preserves_existing_files(tmp_path, monkeypatch):
    folder = tmp_path / "media"
    folder.mkdir()
    existing = folder / "retained.jpg"
    existing.write_bytes(b"retained")
    monkeypatch.setenv("MEDIA_QUOTA_BYTES", "8")
    get_settings.cache_clear()
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, content=b"\xff\xd8\xffsynthetic")

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            blocked = await media.download_attachment(
                client, "https://93.184.216.34/x", folder, "first", 0, "image/jpeg"
            )
            assert blocked[0] is None and "配额" in blocked[2]
            assert not calls
            monkeypatch.setenv("MEDIA_QUOTA_BYTES", "100")
            get_settings.cache_clear()
            accepted = await media.download_attachment(
                client, "https://93.184.216.34/x", folder, "second", 0, "image/jpeg"
            )
            assert accepted[0] and (folder / accepted[0]).read_bytes() == b"\xff\xd8\xffsynthetic"
            assert existing.read_bytes() == b"retained"
            override = await media.download_attachment(
                client, "https://93.184.216.34/x", folder, "third", 0, "image/jpeg", quota_bytes=1
            )
            assert override[0] is None and "配额" in override[2]
    finally:
        get_settings.cache_clear()


def test_capacity_warning_and_full_status_preserve_media(tmp_path):
    p = tmp_path / "evidence.jpg"
    p.write_bytes(b"12345678")
    status = media.capacity_status(tmp_path, quota_bytes=10)
    assert status["state"] == "warning"
    assert status["used_bytes"] == 8 and status["remaining_bytes"] == 2
    assert media.capacity_status(tmp_path, quota_bytes=8)["state"] == "full"
    assert media.capacity_status(tmp_path, quota_bytes=100)["state"] == "ok"
    assert p.read_bytes() == b"12345678"


def image_event():
    return json.loads(
        Path("tests/fixtures/onebot/group_message_image.json").read_text(encoding="utf-8")
    )["event"]


async def test_onebot_retains_quota_error_without_source_url(monkeypatch):
    payload = image_event()

    async def fail(*args, **kwargs):
        return None, "", "磁盘配额不足，剩余81字节"

    monkeypatch.setattr(onebot_wiring, "download_attachment", fail)
    async with httpx.AsyncClient() as client:
        await onebot_wiring.download_onebot_media(
            payload, onebot_wiring.parse_onebot_event(payload), "synthetic", client
        )
    assert payload["_media_download_errors"] == ["quota_exceeded"]
    assert payload["_downloaded"] == [""]


async def test_official_retains_download_error(monkeypatch):
    payload = {
        "id": "synthetic",
        "attachments": [{"url": "https://example.invalid/x", "content_type": "image/jpeg"}],
    }

    async def fail(*args, **kwargs):
        return None, "", "下载失败:ConnectError"

    monkeypatch.setattr(runner, "download_attachment", fail)
    async with httpx.AsyncClient() as client:
        await runner._download_attachments(client, payload)
    assert payload["_media_download_errors"] == ["network_error"]


async def test_diagnostic_survives_pipeline_and_is_visible_without_changing_verdict(
    monkeypatch, tmp_path
):
    from app.adapters.onebot.parser import OneBotMessageSource
    from app.db import SessionLocal
    from app.runtime.pipeline import run_pipeline
    from app.web import auth, routes
    from fastapi import FastAPI

    event = image_event()
    event["message_id"] = 99098761
    event["_downloaded"] = [""]
    event["_media_download_errors"] = ["quota_exceeded", "<script>bad</script>"]
    async with SessionLocal() as session:
        record = await run_pipeline(
            event,
            session,
            message_source=OneBotMessageSource(),
            dedup_key="quota-synthetic-99098761",
        )
        assert record is not None and record.verdict == "record_only"
        assert record.confidence == 0
        assert json.loads(record.detail_json)["media_download_errors"] == ["quota_exceeded"]
        key = record.message_id
    application = FastAPI()
    application.include_router(routes.router)
    token = auth.login("admin", "test-admin-pass")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://isolated.test"
        ) as client:
            client.cookies.set(auth.SESSION_COOKIE, token)
            response = await client.get("/admin/shadow/detail", params={"message_id": key})
            assert response.status_code == 200
            assert "媒体容量不足" in response.text
            assert "<script>bad</script>" not in response.text
    finally:
        auth.logout(token)


async def test_admin_capacity_warning(monkeypatch, tmp_path):
    from app.runtime import pipeline
    from app.web.routes import _media_capacity_banner

    (tmp_path / "media.jpg").write_bytes(b"12345678")
    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    monkeypatch.setenv("MEDIA_QUOTA_BYTES", "10")
    get_settings.cache_clear()
    try:
        assert "媒体容量预警" in await _media_capacity_banner()
    finally:
        get_settings.cache_clear()


def test_health_reports_loaded_quota(monkeypatch, tmp_path):
    from app.main import create_app
    from app.runtime import pipeline
    from fastapi.testclient import TestClient

    monkeypatch.setattr(pipeline, "MEDIA_DIR", tmp_path)
    monkeypatch.setenv("MEDIA_QUOTA_BYTES", str(20 * 1024**3))
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            status = client.get("/healthz").json()["media_storage"]
        assert status["quota_bytes"] == 20 * 1024**3 and status["state"] == "ok"
    finally:
        get_settings.cache_clear()
