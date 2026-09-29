"""Internal nonfriend-page regressions; synthetic accounts, no network calls."""

import csv
import json
import threading
from datetime import UTC, datetime

import pytest
from app.space_inspector.browser import classify_page
from app.space_inspector.contracts import (
    BLOCKED,
    RESTRICTED,
    UNCONFIRMED,
    Group,
    InspectionError,
    Observation,
)
from app.space_inspector.service import run_scan
from app.space_inspector.store import Store

QQ, OTHER, VIEWER = "12345678", "22345678", "98765432"
MESSAGE = "很抱歉,QQ空间相关功能升级维护,暂不支持非好友访问,敬请理解！"


def nonfriend_page():
    return {
        "url": f"https://user.qzone.qq.com/{QQ}",
        "ready": "complete",
        "viewers": [VIEWER],
        "normal_profile": False,
        "panels": [{"paragraphs": ["温馨提示:", MESSAGE, "返回我的空间"], "report_icons": 1}],
    }


def nonfriend_observation():
    return Observation(
        QQ,
        UNCONFIRMED,
        "space_nonfriend_access_unavailable",
        datetime.now(UTC).isoformat(),
        {
            "page_url": f"https://user.qzone.qq.com/{QQ}",
            "viewer_qq": VIEWER,
            "notice": MESSAGE,
            "notice_source": "qzone_nonfriend_page",
            "ready_state": "complete",
            "panel_count": 1,
            "report_icon_count": 1,
        },
    )


def make_store(folder):
    store = Store(folder, create=True)
    store.bind_source("11111111")
    store.bind_viewer(VIEWER)
    store.add_snapshot(Group("55555555", "合成测试群", 2), [QQ, OTHER])
    store.seal_snapshots()
    return store


def test_nonfriend_notice_remains_unconfirmed_with_original_evidence():
    result = classify_page(nonfriend_page(), QQ, VIEWER)
    assert result.status == UNCONFIRMED
    assert result.reason == "space_nonfriend_access_unavailable"
    assert result.evidence["notice"] == MESSAGE
    assert result.evidence["notice_source"] == "qzone_nonfriend_page"


@pytest.mark.parametrize(
    "changes",
    [
        {"viewers": []},
        {"viewers": [OTHER]},
        {"ready": "loading"},
        {"url": f"https://user.qzone.qq.com/{OTHER}"},
        {"normal_profile": True},
        {"panels": []},
        {"panels": [{"paragraphs": ["温馨提示:", MESSAGE, "返回我的空间"], "report_icons": 0}]},
        {"panels": [{"paragraphs": ["温馨提示:", MESSAGE, "返回我的空间"], "report_icons": True}]},
        {
            "panels": [
                {
                    "paragraphs": ["温馨提示:", "当前访问过于频繁，请稍后再试", "返回我的空间"],
                    "report_icons": 1,
                }
            ]
        },
    ],
)
def test_nonfriend_page_does_not_bypass_identity_or_unknown_page_checks(changes):
    raw = nonfriend_page()
    raw.update(changes)
    assert classify_page(raw, QQ, VIEWER).status == BLOCKED


@pytest.mark.parametrize(
    "url,reason",
    [
        ("https://i.qq.com/", "login_required"),
        ("https://waf.tencent.com/501page.html", "platform_access_blocked"),
    ],
)
def test_nonfriend_text_never_overrides_login_or_platform_block(url, reason):
    raw = nonfriend_page()
    raw["url"] = url
    result = classify_page(raw, QQ, VIEWER)
    assert result.status == BLOCKED
    assert result.reason == reason


def test_nonfriend_evidence_survives_store_reopen_and_is_not_restricted_export(tmp_path):
    folder = tmp_path / "task"
    store = make_store(folder)
    try:
        store.save(nonfriend_observation())
    finally:
        store.close()
    reopened = Store(folder)
    try:
        assert reopened.summary()["unconfirmed"] == 1
        assert reopened.summary()["restricted"] == 0
        assert reopened.pending() == [OTHER]
        output = reopened.export()
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        row = next(row for row in report["rows"] if row["qq"] == QQ)
        assert row["evidence"]["notice"] == MESSAGE
        assert row["reason"] == "space_nonfriend_access_unavailable"
        with (output / "restricted.csv").open(encoding="utf-8-sig", newline="") as stream:
            assert list(csv.DictReader(stream)) == []
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "status,change",
    [
        (RESTRICTED, {}),
        (RESTRICTED, {"notice_source": "qzone_top_level_error"}),
        (UNCONFIRMED, {"notice": ""}),
        (UNCONFIRMED, {"report_icon_count": 0}),
        (UNCONFIRMED, {"viewer_qq": OTHER}),
        (UNCONFIRMED, {"notice_source": "qzone_permission_page"}),
    ],
)
def test_nonfriend_store_rejects_incomplete_or_misclassified_evidence(tmp_path, status, change):
    store = make_store(tmp_path / "task")
    original = nonfriend_observation()
    invalid = Observation(
        QQ, status, original.reason, original.checked_at, {**original.evidence, **change}
    )
    try:
        with pytest.raises(InspectionError):
            store.save(invalid)
        assert store.pending() == [QQ, OTHER]
    finally:
        store.close()


def test_scan_resumes_nonfriend_member_and_keeps_old_blocked_history(tmp_path):
    store = make_store(tmp_path / "task")
    calls = []

    class Reader:
        def inspect(self, qq, viewer):
            calls.append(qq)
            if qq == QQ:
                return classify_page(nonfriend_page(), qq, viewer)
            return classify_page(
                {
                    "url": f"https://user.qzone.qq.com/{qq}",
                    "ready": "complete",
                    "viewers": [viewer],
                    "normal_profile": True,
                    "panels": [],
                },
                qq,
                viewer,
            )

    try:
        store.save(classify_page({}, QQ, VIEWER))
        run_scan(
            store, Reader(), VIEWER, threading.Event(), lambda _: None, delay=0, max_checks=300
        )
        assert calls == [QQ, OTHER]
        assert not store.pending()
        assert store.summary()["unconfirmed"] == 2
        assert store.summary()["restricted"] == 0
        visits = store.db.execute(
            "SELECT status FROM visits WHERE qq=? ORDER BY id", (QQ,)
        ).fetchall()
        assert [row[0] for row in visits] == [BLOCKED, UNCONFIRMED]
    finally:
        store.close()
