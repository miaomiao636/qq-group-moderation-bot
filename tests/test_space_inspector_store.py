"""Internal storage regressions using synthetic identities and temporary SQLite files."""

import csv
import hashlib
import json
import sqlite3

import pytest
from app.space_inspector.contracts import (
    BLOCKED,
    RESTRICTED,
    UNCONFIRMED,
    Group,
    InspectionError,
    Observation,
)
from app.space_inspector.store import Store

SOURCE = "11111111"
VIEWER = "22222222"
MEMBER = "33333333"
OTHER = "44444444"
GROUP = "55555555"
STAMP = "2026-09-21T12:00:00+00:00"
NOTICE = "您访问的空间存在违规信息,已被多名用户举报,暂时无法查看！"


def evidence(qq=MEMBER, restricted=False):
    return {
        "page_url": "https://user.qzone.qq.com/" + qq,
        "viewer_qq": VIEWER,
        "notice": NOTICE if restricted else "",
        "notice_source": "qzone_top_level_error" if restricted else "qzone_profile",
        "ready_state": "complete",
        "panel_count": 1 if restricted else 0,
        "report_icon_count": 1 if restricted else 0,
    }


def make_store(folder, name="测试群", members=None, declared=2):
    store = Store(folder, create=True)
    store.bind_source(SOURCE)
    store.bind_viewer(VIEWER)
    store.add_snapshot(Group(GROUP, name, declared), members or [MEMBER, OTHER])
    store.seal_snapshots()
    return store


def observation(status=RESTRICTED, qq=MEMBER, **changes):
    values = dict(
        qq=qq,
        status=status,
        reason="exact_notice" if status == RESTRICTED else "no_exact_notice",
        checked_at=STAMP,
        evidence=evidence(qq, status == RESTRICTED),
    )
    values.update(changes)
    return Observation(**values)


def test_resume_retains_source_viewer_progress_and_counts(tmp_path):
    folder = tmp_path / "task"
    store = make_store(folder)
    store.save(observation())
    store.close()
    resumed = Store(folder)
    try:
        assert resumed.metadata["source_self_id"] == SOURCE
        assert resumed.metadata["viewer_qq"] == VIEWER
        assert resumed.metadata["prepared"] is True
        assert resumed.pending() == [OTHER]
        assert resumed.summary() == {
            "total": 2,
            "checked": 1,
            "restricted": 1,
            "unconfirmed": 0,
            "pending": 1,
        }
    finally:
        resumed.close()


def test_cross_group_member_checked_once_but_exported_per_group(tmp_path):
    store = Store(tmp_path / "task", create=True)
    try:
        store.bind_source(SOURCE)
        store.bind_viewer(VIEWER)
        store.add_snapshot(Group(GROUP, "甲群", 1), [MEMBER])
        store.add_snapshot(Group("66666666", "乙群", 1), [MEMBER])
        store.seal_snapshots()
        assert store.pending() == [MEMBER]
        store.save(observation())
        assert store.summary()["total"] == 1
        rows = store.rows()
        assert len(rows) == 2
        assert {row["group_name"] for row in rows} == {"甲群", "乙群"}
        export = store.export()
        with (export / "restricted.csv").open(encoding="utf-8-sig", newline="") as stream:
            assert len(list(csv.DictReader(stream))) == 2
    finally:
        store.close()


def test_csv_formula_protection_pending_and_unconfirmed_not_restricted(tmp_path):
    store = make_store(tmp_path / "task", name='  =HYPERLINK("bad")')
    try:
        store.save(observation(UNCONFIRMED))
        first = store.export()
        second = store.export()
        assert first != second
        assert (first / "report.csv").read_bytes().startswith(b"\xef\xbb\xbf")
        with (first / "report.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 2
        assert all(row["群名"].startswith("'") for row in rows)
        with (first / "restricted.csv").open(encoding="utf-8-sig", newline="") as stream:
            assert list(csv.DictReader(stream)) == []
        report = json.loads((first / "report.json").read_text(encoding="utf-8"))
        assert report["summary"]["unconfirmed"] == 1
        assert report["summary"]["pending"] == 1
    finally:
        store.close()


def test_foreign_database_is_rejected_without_rewriting(tmp_path):
    folder = tmp_path / "foreign"
    folder.mkdir()
    database = folder / "task.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE user_data(value TEXT)")
        connection.execute("INSERT INTO user_data VALUES ('preserve')")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(InspectionError):
        Store(folder)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not (folder / "task.sqlite3-wal").exists()


def test_identity_change_rejected_before_and_after_resume(tmp_path):
    folder = tmp_path / "task"
    store = make_store(folder)
    for method in (store.bind_source, store.bind_viewer):
        with pytest.raises(InspectionError):
            method("99999999")
    store.close()
    store = Store(folder)
    try:
        with pytest.raises(InspectionError):
            store.bind_viewer("99999999")
        assert store.pending() == [MEMBER, OTHER]
    finally:
        store.close()


def test_blocked_retry_preserves_history_and_complete_not_requeued(tmp_path):
    folder = tmp_path / "task"
    store = make_store(folder)
    try:
        store.save(observation(BLOCKED))
        assert store.pending() == [MEMBER, OTHER]
        store.save(observation())
        with pytest.raises(InspectionError):
            store.save(observation(UNCONFIRMED))
        with sqlite3.connect(folder / "task.sqlite3") as connection:
            assert connection.execute("SELECT status FROM visits ORDER BY id").fetchall() == [
                (BLOCKED,),
                (RESTRICTED,),
            ]
        assert store.pending() == [OTHER]
    finally:
        store.close()


def test_partial_snapshot_cannot_run_until_explicitly_sealed(tmp_path):
    folder = tmp_path / "task"
    store = Store(folder, create=True)
    store.bind_source(SOURCE)
    store.add_snapshot(Group(GROUP, "甲", 2), [MEMBER])
    store.close()
    store = Store(folder)
    try:
        assert store.metadata["prepared"] is False
        partial = json.loads((store.export() / "report.json").read_text(encoding="utf-8"))
        assert partial["metadata"]["prepared"] is False
        with pytest.raises(InspectionError):
            store.pending()
        store.bind_viewer(VIEWER)
        with pytest.raises(InspectionError):
            store.save(observation())
        store.seal_snapshots()
        assert store.pending() == [MEMBER]
        with pytest.raises(InspectionError):
            store.add_snapshot(Group("66666666", "乙", 1), [OTHER])
        report = json.loads((store.export() / "report.json").read_text(encoding="utf-8"))
        assert report["groups"][0]["declared_count"] == 2
        assert report["groups"][0]["snapshot_count"] == 1
    finally:
        store.close()


def test_snapshot_repeat_is_idempotent_but_changes_rejected(tmp_path):
    store = Store(tmp_path / "task", create=True)
    try:
        store.bind_source(SOURCE)
        group = Group(GROUP, "甲", 2)
        store.add_snapshot(group, [MEMBER, OTHER])
        store.add_snapshot(group, [OTHER, MEMBER, MEMBER])
        for changed, members in ((Group(GROUP, "改名", 2), [MEMBER, OTHER]), (group, [MEMBER])):
            with pytest.raises(InspectionError):
                store.add_snapshot(changed, members)
        assert store.summary()["total"] == 2
    finally:
        store.close()


def test_evidence_rejects_credentials_html_unbound_viewer_and_query(tmp_path):
    store = make_store(tmp_path / "task")
    try:
        for invalid in (
            {**evidence(), "cookie": "SECRET"},
            {**evidence(), "notice": "<html>SECRET</html>"},
            {**evidence(), "viewer_qq": "99999999"},
            {**evidence(), "page_url": "https://user.qzone.qq.com/33333333?token=SECRET"},
        ):
            with pytest.raises(InspectionError) as raised:
                store.save(observation(UNCONFIRMED, evidence=invalid))
            assert "SECRET" not in str(raised.value)
        assert store.summary()["checked"] == 0
    finally:
        store.close()


def test_invalid_observation_and_row_limit_are_rejected(tmp_path):
    store = make_store(tmp_path / "task")
    try:
        for changed in (
            {"qq": "99999999"},
            {"status": "NORMAL"},
            {"reason": "cookie=SECRET"},
            {"checked_at": "yesterday"},
        ):
            with pytest.raises(InspectionError):
                store.save(observation(**changed))
        for limit in (0, -1, 1001, True):
            with pytest.raises(InspectionError):
                store.rows(limit)
    finally:
        store.close()


def test_unconfirmed_requires_a_completed_profile_for_bound_viewer(tmp_path):
    store = make_store(tmp_path / "task")
    try:
        for change in (
            {"page_url": ""},
            {"viewer_qq": ""},
            {"ready_state": "loading"},
            {"notice_source": "unrecognized_page"},
            {"notice": NOTICE},
            {"panel_count": 1},
            {"report_icon_count": 1},
        ):
            with pytest.raises(InspectionError):
                store.save(observation(UNCONFIRMED, evidence={**evidence(), **change}))
        assert store.summary()["checked"] == 0
    finally:
        store.close()


def test_stored_schema_or_metadata_corruption_is_rejected(tmp_path):
    for damage in ("DROP TABLE visits", "UPDATE meta SET value='bad' WHERE key='source_self_id'"):
        folder = tmp_path / str(len(list(tmp_path.iterdir())))
        store = make_store(folder)
        store.close()
        with sqlite3.connect(folder / "task.sqlite3") as connection:
            connection.execute(damage)
        with pytest.raises(InspectionError):
            Store(folder)


def test_cannot_overwrite_existing_task_or_create_on_resume(tmp_path):
    folder = tmp_path / "task"
    store = make_store(folder)
    store.close()
    with pytest.raises(InspectionError):
        Store(folder, create=True)
    absent = tmp_path / "absent"
    with pytest.raises(InspectionError):
        Store(absent)
    assert not absent.exists()
