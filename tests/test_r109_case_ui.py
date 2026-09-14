"""R-109：归档页面和案件附件回看的真实 HTTP 契约。

只用迁移后的临时 SQLite 与合成证据，不进入应用 lifespan 或调用外部服务。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode

import pytest
from app.adapters.qq_official.media import safe_filename
from app.cases.models import Case, ViolationRecord
from app.db import SessionLocal
from app.web import auth
from fastapi.testclient import TestClient


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[tuple[dict[str, str | None], dict[str, str]]] = []
        self.current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.current = {}
            self.forms.append((values, self.current))
        elif tag == "input" and self.current is not None and values.get("name"):
            self.current[str(values["name"])] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


@dataclass(frozen=True)
class _CaseData:
    case_id: int
    case_no: str
    group: str
    violation_id: int


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    from app.main import app

    http = TestClient(app, raise_server_exceptions=False)
    try:
        yield http
    finally:
        token = http.cookies.get(auth.SESSION_COOKIE)
        if token:
            auth.logout(token)
        http.close()


def _login(client: TestClient) -> str:
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test-admin-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return auth.csrf_token(client.cookies[auth.SESSION_COOKIE])


_DEFAULT_SNAPSHOT = object()


def _seed(
    *, archived: bool = True, snapshot: Any = _DEFAULT_SNAPSHOT, purged: bool = False
) -> _CaseData:
    async def seed() -> _CaseData:
        marker = uuid.uuid4().hex
        now = datetime.now(UTC)
        identity = {
            "group_openid": f"synthetic-case-group-{marker}",
            "member_openid": f"synthetic-case-member-{marker}",
            "provider": "onebot",
        }
        async with SessionLocal() as session:
            record = ViolationRecord(
                **identity,
                message_id=f"synthetic-case-message-{marker}",
                category="ad",
                confidence=0.95,
                message_snapshot_json=json.dumps(
                    {"text": "合成证据"} if snapshot is _DEFAULT_SNAPSHOT else snapshot,
                    ensure_ascii=False,
                ),
            )
            session.add(record)
            await session.flush()
            case = Case(
                **identity,
                case_no=f"R-{marker[:16]}",
                status="CLOSED",
                violation_ids_json="[]" if purged else json.dumps([record.id]),
                audit_json=json.dumps(
                    {"lifecycle_purged_at": now.isoformat()}
                    if purged
                    else {"history": "synthetic audit"}
                ),
                created_at=now - timedelta(days=20),
                closed_at=now - timedelta(days=16),
                archived=archived,
                archived_at=now if archived else None,
            )
            session.add(case)
            await session.flush()
            record.case_id = case.id
            await session.commit()
            return _CaseData(case.id, case.case_no, case.group_openid, record.id)

    return asyncio.run(seed())


def _forms(html: str) -> _Forms:
    parser = _Forms()
    parser.feed(html)
    return parser


def test_archived_filter_submission_stays_in_archive(client: TestClient) -> None:
    data = _seed()
    _login(client)
    response = client.get("/admin/", params={"archived": "1", "group": data.group})
    assert data.case_no in response.text
    form_attrs, fields = next(
        (attrs, fields)
        for attrs, fields in _forms(response.text).forms
        if attrs.get("method", "").lower() == "get" and "group" in fields
    )
    filtered = client.get((form_attrs.get("action") or "/admin/") + "?" + urlencode(fields))
    assert filtered.status_code == 200
    assert data.case_no in filtered.text
    assert "已归档案件" in filtered.text


def test_archived_case_exposes_authenticated_restore_form(client: TestClient) -> None:
    data = _seed()
    _login(client)
    response = client.get(f"/admin/cases/{data.case_id}")
    assert response.status_code == 200
    restore = [
        fields
        for attrs, fields in _forms(response.text).forms
        if attrs.get("action") == f"/admin/cases/{data.case_id}/unarchive"
        and attrs.get("method", "").lower() == "post"
    ]
    assert restore and restore[0].get("csrf")


def test_unarchive_requires_login_and_csrf(client: TestClient) -> None:
    data = _seed()
    path = f"/admin/cases/{data.case_id}/unarchive"
    assert client.post(path).status_code == 401
    _login(client)
    assert client.post(path).status_code == 403

    async def unchanged() -> None:
        async with SessionLocal() as session:
            case = await session.get(Case, data.case_id)
            assert case is not None and case.archived is True

    asyncio.run(unchanged())


def test_unarchive_only_restores_visibility_not_closed_state_or_evidence(
    client: TestClient,
) -> None:
    data = _seed()
    csrf = _login(client)
    for _ in range(2):
        response = client.post(
            f"/admin/cases/{data.case_id}/unarchive",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303

    async def preserved() -> None:
        async with SessionLocal() as session:
            case = await session.get(Case, data.case_id)
            assert case is not None
            assert not case.archived and case.archived_at is None
            assert case.status == "CLOSED"
            assert json.loads(case.violation_ids_json) == [data.violation_id]
            assert json.loads(case.audit_json) == {"history": "synthetic audit"}
            assert await session.get(ViolationRecord, data.violation_id) is not None

    asyncio.run(preserved())


def test_unarchive_missing_case_does_not_claim_success(client: TestClient) -> None:
    csrf = _login(client)
    response = client.post(
        "/admin/cases/2147483647/unarchive", data={"csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 404


def test_logically_purged_case_does_not_offer_restore(client: TestClient) -> None:
    data = _seed(purged=True)
    _login(client)
    response = client.get(f"/admin/cases/{data.case_id}")
    assert response.status_code == 200
    assert data.case_no in response.text
    assert not any(
        attrs.get("action") == f"/admin/cases/{data.case_id}/unarchive"
        for attrs, _ in _forms(response.text).forms
    )


def test_logically_purged_case_rejects_direct_restore_without_mutation(
    client: TestClient,
) -> None:
    data = _seed(purged=True)
    csrf = _login(client)

    async def snapshot_case() -> tuple[Any, ...]:
        async with SessionLocal() as session:
            case = await session.get(Case, data.case_id)
            assert case is not None
            return (
                case.archived,
                case.archived_at,
                case.status,
                case.violation_ids_json,
                case.audit_json,
            )

    before = asyncio.run(snapshot_case())
    response = client.post(
        f"/admin/cases/{data.case_id}/unarchive", data={"csrf": csrf}, follow_redirects=False
    )
    assert response.status_code == 409
    assert asyncio.run(snapshot_case()) == before


@pytest.mark.parametrize("extension", [".jpg", ".mkv", ".pdf", ".amr"])
def test_case_media_links_accept_managed_downloader_filenames(
    client: TestClient, extension: str
) -> None:
    filename = safe_filename("synthetic-evidence-message", 0) + extension
    data = _seed(snapshot={"text": "", "attachments": [{"filename": filename}]})
    _login(client)
    response = client.get(f"/admin/cases/{data.case_id}")
    assert response.status_code == 200
    assert f'href="/admin/media/{filename}"' in response.text


@pytest.mark.parametrize("malformed", [None, "legacy attachment metadata", 42])
def test_bad_attachment_metadata_does_not_hide_other_valid_evidence(
    client: TestClient, malformed: Any
) -> None:
    filename = safe_filename("synthetic-valid-image", 0) + ".jpg"
    data = _seed(
        snapshot={
            "text": "合成证据仍可查看",
            "attachments": [malformed, {"filename": filename}],
        }
    )
    _login(client)
    response = client.get(f"/admin/cases/{data.case_id}")
    assert response.status_code == 200
    assert "合成证据仍可查看" in response.text
    assert f'href="/admin/media/{filename}"' in response.text


@pytest.mark.parametrize("filename", ["../private.jpg", "/private/image.jpg", "test.html"])
def test_case_media_links_reject_unmanaged_paths(client: TestClient, filename: str) -> None:
    data = _seed(snapshot={"text": "", "attachments": [{"filename": filename}]})
    _login(client)
    response = client.get(f"/admin/cases/{data.case_id}")
    assert response.status_code == 200
    assert 'href="/admin/media/' not in response.text


@pytest.mark.parametrize("attachments", [None, {}, "legacy attachment field", 42])
def test_non_list_attachments_do_not_hide_case_text(client: TestClient, attachments: Any) -> None:
    data = _seed(snapshot={"text": "合成正文可以核对", "attachments": attachments})
    _login(client)
    response = client.get(f"/admin/cases/{data.case_id}")
    assert response.status_code == 200
    assert "合成正文可以核对" in response.text
    assert 'href="/admin/media/' not in response.text


@pytest.mark.parametrize("snapshot", [None, [], "legacy snapshot", 42])
def test_non_object_snapshot_does_not_crash_case_detail(client: TestClient, snapshot: Any) -> None:
    data = _seed(snapshot=snapshot)
    _login(client)
    response = client.get(f"/admin/cases/{data.case_id}")
    assert response.status_code == 200
    assert data.case_no in response.text
    assert 'href="/admin/media/' not in response.text
