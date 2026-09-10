"""Admin backup controls report only completed snapshots and render real CSRF."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_admin_web import extract_csrf


@pytest.fixture
def logged_in():
    from app.main import app

    with TestClient(app) as browser:
        browser.post("/admin/login", data={"username": "admin", "password": "test-admin-pass"})
        yield browser


def test_settings_forms_render_csrf_and_backup_uses_config(logged_in, monkeypatch) -> None:
    from app.config import get_settings

    seen = []

    def fake_backup(url):
        seen.append(url)
        return Path("moderation-test-only.db")

    monkeypatch.setattr("app.reports.backup.backup_sqlite", fake_backup)
    page = logged_in.get("/admin/settings")
    assert "{csrf}" not in page.text
    response = logged_in.post(
        "/admin/settings/backup", data={"csrf": extract_csrf(page.text)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert seen == [get_settings().database_url]


def test_failed_backup_is_not_reported_as_success(logged_in, monkeypatch) -> None:
    def fail_backup(_url):
        raise OSError("private path must not leak")

    monkeypatch.setattr("app.reports.backup.backup_sqlite", fail_backup)
    page = logged_in.get("/admin/settings")
    response = logged_in.post(
        "/admin/settings/backup", data={"csrf": extract_csrf(page.text)}, follow_redirects=False
    )
    assert response.status_code == 503
    assert "private path" not in response.text
    assert "未完成" in response.text
