"""Independent local task persistence; fixtures contain synthetic accounts only."""

import json

import pytest
from app.phone_inspector.pages import InspectionError
from app.phone_inspector.storage import Store


def task(tmp_path):
    store = Store(tmp_path / "task", create=True)
    store.bind(
        {
            "group_id": "12345678",
            "group_name": "=虚构群",
            "device_hash": "fake",
            "android_user": 999,
            "qq_version": "9.3.60",
        }
    )
    return store


def test_restart_preserves_warning_and_pending_but_never_marks_complete(tmp_path):
    store = task(tmp_path)
    pass_id = store.begin_pass(2)
    first = store.start_visit(pass_id, 0, 0)
    store.finish_visit(first, "12345601", "WARNING_OBSERVED")
    store.start_visit(pass_id, 0, 1)
    store.close()
    store = Store(tmp_path / "task")
    next_pass = store.begin_pass(2)
    visit = store.start_visit(next_pass, 0, 0)
    store.finish_visit(visit, "12345601", "WARNING_NOT_OBSERVED_THIS_VISIT")
    assert store.stats()["checked"] == 1
    assert store.stats()["warnings"] == 1
    assert store.stats()["unresolved"] == 1
    assert store.observations()[0]["result"] == "WARNING_OBSERVED"
    assert (
        store.db.execute("SELECT status FROM passes WHERE id=?", (pass_id,)).fetchone()[0]
        == "PAUSED"
    )
    store.close()


def test_identity_mismatch_cannot_merge_tasks(tmp_path):
    store = task(tmp_path)
    with pytest.raises(InspectionError):
        store.bind(
            {
                "group_id": "87654321",
                "device_hash": "fake",
                "android_user": 999,
                "qq_version": "9.3.60",
            }
        )
    assert store.metadata()["group_id"] == "12345678"
    store.close()


def test_without_qq_cannot_mark_warning_or_overwrite_finished_visit(tmp_path):
    store = task(tmp_path)
    visit = store.start_visit(store.begin_pass(None), 0, 0)
    with pytest.raises(InspectionError):
        store.finish_visit(visit, None, "WARNING_OBSERVED")
    store.finish_visit(visit, "12345601", "WARNING_OBSERVED")
    with pytest.raises(InspectionError):
        store.finish_visit(visit, "12345602", "WARNING_OBSERVED")
    store.close()


def test_export_is_separate_and_does_not_erase_earlier_warning(tmp_path):
    store = task(tmp_path)
    pid = store.begin_pass(2)
    for result in ("WARNING_OBSERVED", "WARNING_NOT_OBSERVED_THIS_VISIT"):
        visit = store.start_visit(pid, 0, 0)
        store.finish_visit(visit, "12345601", result)
    export = store.export()
    assert "'=虚构群" in (export / "异常提示账号.csv").read_text(encoding="utf-8-sig")
    report = json.loads((export / "巡检报告.json").read_text(encoding="utf-8"))
    assert len(report["warnings"]) == 1
    assert report["stats"]["latest_pass"]["status"] == "RUNNING"
    assert store.export() != export
    store.close()


def test_foreign_database_not_accepted(tmp_path):
    import sqlite3

    folder = tmp_path / "foreign"
    folder.mkdir()
    with sqlite3.connect(folder / "inspection.sqlite3") as db:
        db.execute("CREATE TABLE private(value)")
    with pytest.raises(InspectionError):
        Store(folder)
