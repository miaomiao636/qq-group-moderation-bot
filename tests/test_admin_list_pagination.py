"""UI-PAGING-20260921: bounded admin lists with unchanged full-data contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from app.cases.models import Case
from app.core.routing import GroupProviderRoute
from app.db import Base
from app.models import AllowlistMember, GroupActionOwner, HiddenGroup, ProviderGroupSettings
from app.runtime.models import ShadowDecision
from app.web import auth, routes
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def ui(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin-pages.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(routes, "SessionLocal", factory)
    application = FastAPI()
    application.include_router(routes.router)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    token = auth.login("admin", "test-admin-pass")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            client.cookies.set(auth.SESSION_COOKIE, token)
            yield client, factory, statements
    finally:
        auth.logout(token)
        await engine.dispose()


async def seed(factory, kind, count=45):
    async with factory() as session:
        for index in range(count):
            if kind == "groups":
                row = ProviderGroupSettings(
                    provider="onebot", external_group_id=f"G{index:03}", name=f"演示群{index:03}"
                )
            elif kind == "members":
                row = AllowlistMember(external_user_id=f"81000{index:04}", note=f"名单{index:03}")
            else:
                row = Case(
                    case_no=f"PAGE-{index:03}",
                    group_openid="G-PAGE",
                    member_openid=f"U{index:03}",
                    created_at=datetime(2026, 9, 21, tzinfo=UTC),
                    archived=index == 0,
                )
            session.add(row)
        await session.commit()


@pytest.mark.parametrize(
    ("kind", "path", "parameter", "pattern"),
    [
        ("groups", "/admin/groups", "page", r"<code>(G\d{3})</code>"),
        ("members", "/admin/allowlist", "member_page", r"<code>(81000\d{4})</code>"),
        ("pending", "/admin/reports", "page", r">(PAGE-\d{3})</a>"),
    ],
)
async def test_lists_are_bounded_and_all_pages_are_reachable(ui, kind, path, parameter, pattern):
    client, factory, _ = ui
    await seed(factory, kind)
    found = []
    for page, expected in [(1, 20), (2, 20), (3, 5)]:
        response = await client.get(path, params={parameter: page})
        assert response.status_code == 200
        identifiers = re.findall(pattern, response.text)
        assert len(identifiers) == expected
        assert "共 45 条" in response.text
        assert f"第 {page}/3 页" in response.text
        found.extend(identifiers)
    assert len(set(found)) == 45
    assert found == sorted(found)
    response = await client.get(path, params={parameter: 999999})
    assert re.findall(pattern, response.text) == found[-5:]


async def test_group_counts_keep_provider_defaults_and_hidden_configuration(ui):
    client, factory, _ = ui
    async with factory() as session:
        session.add_all(
            [
                ProviderGroupSettings(
                    provider="onebot", external_group_id="same", action_enabled=True
                ),
                ProviderGroupSettings(
                    provider="qq_official",
                    external_group_id="same",
                    moderation_enabled=False,
                    action_enabled=True,
                ),
                HiddenGroup(provider="onebot", external_group_id="same"),
                GroupActionOwner(external_group_id="same", provider="onebot"),
                GroupProviderRoute(
                    message_provider="onebot", external_group_id="same", action_provider="onebot"
                ),
                *[
                    ShadowDecision(
                        message_id=f"seen-{i}",
                        provider="onebot",
                        external_group_id="seen-only",
                        group_openid="seen-only",
                        member_openid="member",
                        verdict="allow",
                    )
                    for i in range(2)
                ],
                ShadowDecision(
                    message_id="empty",
                    external_group_id="",
                    group_openid="",
                    member_openid="member",
                    verdict="allow",
                ),
            ]
        )
        await session.commit()
    for query in ("", "?show_hidden=1&page=999"):
        response = await client.get("/admin/groups" + query)
        for label, value in [
            ("后台已记录群", 3),
            ("审核开启", 2),
            ("仅审核", 1),
            ("审核＋真实动作已配置", 1),
            ("审核关闭", 1),
            ("已隐藏群", 1),
        ]:
            assert re.search(
                rf">{value}</div>\s*<div class=muted>{re.escape(label)}</div>", response.text
            )
        assert "不代表实际动作已生效" in response.text
    hidden = (await client.get("/admin/groups?show_hidden=1")).text
    assert 'name=provider value="onebot"' in hidden
    assert 'name=provider value="qq_official"' not in hidden
    assert auth.csrf_token(client.cookies[auth.SESSION_COOKIE]) in hidden
    async with factory() as session:
        settings = (await session.scalars(select(ProviderGroupSettings))).all()
        assert len(settings) == 2  # Shadow-only GET does not create settings.
        assert all(row.action_enabled for row in settings)
        assert (await session.get(GroupActionOwner, "same")).provider == "onebot"


async def test_member_paging_does_not_limit_export_or_import_preview(ui):
    client, factory, _ = ui
    await seed(factory, "members")
    async with factory() as session:
        row = await session.get(AllowlistMember, 45)
        row.enabled = False
        session.add(AllowlistMember(provider="qq_official", external_user_id="openid-private"))
        await session.commit()
    response = await client.get("/admin/allowlist?member_page=2")
    assert len(re.findall(r"<code>81000\d{4}</code>", response.text)) == 20
    exported = await client.get("/admin/allowlist/members/export?member_page=2")
    assert all(f"81000{i:04}" in exported.text for i in range(44))
    assert "810000044" not in exported.text
    assert "openid-private" not in exported.text
    csrf = auth.csrf_token(client.cookies[auth.SESSION_COOKIE])
    preview = await client.post(
        "/admin/allowlist/members/import",
        data={"csrf": csrf, "text": "810000000\n"},
    )
    assert preview.status_code == 200
    assert "810000043" in preview.text  # Beyond page 1 participates in full-table sync.
    async with factory() as session:
        assert (await session.get(AllowlistMember, 44)).enabled is True


async def test_pending_pages_preserve_totals_scope_and_dashboard_filters(ui):
    client, factory, _ = ui
    await seed(factory, "pending")
    async with factory() as session:
        session.add(
            Case(case_no="NOT-PENDING", group_openid="G", member_openid="U", status="CLOSED")
        )
        await session.commit()
    report = (await client.get("/admin/reports?page=2")).text
    assert '"cases_pending_review": 45' in unescape(report)
    assert "待人工处理清单（45）" in report
    assert "NOT-PENDING" not in report
    dashboard = (
        await client.get("/admin/?pending_page=2&page=1&status=CLOSED&archived=1&group=G")
    ).text
    assert "待人工处理（45）" in dashboard
    assert "PAGE-000" not in dashboard and "PAGE-020" in dashboard
    links = [unescape(link) for link in re.findall(r'href="([^"]+)"', dashboard)]
    next_pending = next(
        link for link in links if parse_qs(urlsplit(link).query).get("pending_page") == ["3"]
    )
    query = parse_qs(urlsplit(next_pending).query)
    assert query["status"] == ["CLOSED"] and query["group"] == ["G"]
    assert query["archived"] == ["1"] and query["page"] == ["1"]
    async with factory() as session:
        cases = (await session.scalars(select(Case))).all()
        assert sum(row.status == "PENDING_REVIEW" for row in cases) == 45
        assert cases[0].archived is True


async def test_case_pager_and_filter_keep_the_pending_page(ui):
    client, factory, _ = ui
    await seed(factory, "pending", 65)
    response = await client.get("/admin/?pending_page=2&pending_page_size=20")
    links = [unescape(link) for link in re.findall(r'href="([^"]+)"', response.text)]
    next_cases = next(link for link in links if parse_qs(urlsplit(link).query).get("page") == ["2"])
    query = parse_qs(urlsplit(next_cases).query)
    assert query["pending_page"] == ["2"] and query["pending_page_size"] == ["20"]
    assert '<input type=hidden name="pending_page" value="2">' in response.text
    assert '<input type=hidden name="pending_page_size" value="20">' in response.text
    second = await client.get(next_cases, follow_redirects=True)
    assert "PAGE-020" in second.text
    assert "<details id=pending-list open>" in second.text


@pytest.mark.parametrize("path", ["/admin/groups", "/admin/allowlist", "/admin/reports"])
async def test_empty_lists_and_unauthenticated_access(ui, path):
    client, _, _ = ui
    response = await client.get(path)
    assert response.status_code == 200
    assert "共 0 条" in response.text and "第 1/1 页" in response.text
    client.cookies.clear()
    response = await client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


async def test_query_size_is_bounded_and_page_links_escape_parameters(ui):
    client, factory, statements = ui
    await seed(factory, "members", 65)
    for size, expected in [(50, 50), (0, 20), (999999999, 20)]:
        statements.clear()
        response = await client.get(
            "/admin/allowlist",
            params={
                "member_page": -4,
                "member_page_size": size,
                "extra": '"><script>alert(1)</script>',
            },
        )
        assert len(re.findall(r"<code>81000\d{4}</code>", response.text)) == expected
        member_reads = [
            sql.lower()
            for sql in statements
            if "from allowlist_members" in sql.lower() and "count(" not in sql.lower()
        ]
        assert member_reads and all("limit" in sql for sql in member_reads)
        assert "<script>alert(1)</script>" not in response.text
    assert (await client.get("/admin/allowlist?member_page=bad")).status_code == 422
