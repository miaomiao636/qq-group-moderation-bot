"""Internal export usability regressions; synthetic snapshots only, no QQ access."""

import csv
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.space_inspector import store as store_module
from app.space_inspector.contracts import Group, InspectionError
from app.space_inspector.history import list_tasks
from app.space_inspector.service import Service
from app.space_inspector.store import Store

from tests.test_space_inspector_store import GROUP, MEMBER, OTHER, make_store, observation


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def files_hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}


def test_export_is_one_level_outside_task_and_old_exports_survive(tmp_path):
    folder = tmp_path / "tasks" / "20260923T120000Z-0123abcd"
    store = make_store(folder, name="测试交流群")
    try:
        store.save(observation())
        old = store.export()
        old_hashes = files_hashes(old)
        history_before = list_tasks(folder.parent)
        destination = tmp_path / "QQ空间巡检结果"
        first = store.export(destination)
        second = store.export(destination)
        assert first.parent == second.parent == destination
        assert first != second
        assert f"测试交流群_群{GROUP}" in first.name
        assert all(p.is_file() for p in first.iterdir())
        assert (first / "COMPLETE").is_file()
        assert files_hashes(old) == old_hashes
        assert list_tasks(folder.parent) == history_before
        assert store.pending() == [OTHER]
        assert json.loads((first / "report.json").read_text("utf-8"))["task_id"] == folder.name
        assert read_csv(first / f"测试交流群_群{GROUP}_全部成员.csv") == read_csv(
            first / "report.csv"
        )
        assert len(read_csv(first / f"测试交流群_群{GROUP}_观察到限制.csv")) == 1
        assert folder.name in (first / "说明.txt").read_text("utf-8-sig")
        assert "测试交流群" in store.task_label()
        assert GROUP in store.task_label()
    finally:
        store.close()


def test_multi_group_reports_preserve_memberships_with_duplicate_names(tmp_path):
    store = Store(tmp_path / "task", create=True)
    try:
        store.bind_source("11111111")
        store.bind_viewer("22222222")
        store.add_snapshot(Group(GROUP, "同名群", 2), [MEMBER, OTHER])
        store.add_snapshot(Group("66666666", "同名群", 1), [MEMBER])
        store.seal_snapshots()
        store.save(observation())
        output = store.export(tmp_path / "exports")
        assert "等2群" in output.name
        assert len(read_csv(output / "report.csv")) == 3
        assert len(read_csv(output / "restricted.csv")) == 2
        for group_id, total in ((GROUP, 2), ("66666666", 1)):
            rows = read_csv(output / f"同名群_群{group_id}_全部成员.csv")
            assert len(rows) == total
            assert {row["群号"] for row in rows} == {group_id}
            assert len(read_csv(output / f"同名群_群{group_id}_观察到限制.csv")) == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    "name",
    [
        "../../外部",
        'C:\\资料/<群>:"|?*',
        "NUL.txt",
        "CON",
        "COM¹.txt",
        "LPT².csv",
        "群. ",
        "群😀" * 160,
        "=SUM(1,2)",
    ],
)
def test_untrusted_group_names_cannot_escape_or_make_invalid_windows_names(tmp_path, name):
    store = make_store(tmp_path / "task", name=name)
    try:
        destination = tmp_path / "exports"
        output = store.export(destination)
        assert output.parent == destination
        for path in (output, *output.iterdir()):
            assert not any(c in path.name for c in '<>:"/\\|?*')
            assert not path.name.endswith((".", " "))
            assert len(path.name.encode("utf-16-le")) <= 220
        per_group = next(output.glob("*_全部成员.csv"))
        rows = read_csv(per_group)
        assert len(rows) == 2
        assert rows[0]["群名"] == ("'" + name if name.startswith("=") else name)
        assert read_csv(next(output.glob("*_观察到限制.csv"))) == []
    finally:
        store.close()


def test_export_write_failure_is_not_published_and_keeps_task(tmp_path, monkeypatch):
    store = make_store(tmp_path / "task")
    original = Path.open

    def fail_report(path, *args, **kwargs):
        if path.name.endswith("_全部成员.csv"):
            raise OSError("synthetic disk failure")
        return original(path, *args, **kwargs)

    try:
        monkeypatch.setattr(Path, "open", fail_report)
        with pytest.raises(InspectionError, match="导出未完成"):
            store.export(tmp_path / "exports")
        assert not list((tmp_path / "exports").rglob("COMPLETE"))
        assert store.pending() == [MEMBER, OTHER]
        assert not store.db.in_transaction
    finally:
        store.close()


def test_service_exports_to_fixed_result_root_without_changing_task(tmp_path):
    service = Service.__new__(Service)
    service._store = make_store(tmp_path / "tasks" / "20260923T120000Z-0123abcd")
    service.current_folder = service._store.folder
    service.export_root = tmp_path / "QQ空间巡检结果"
    try:
        first = service.export()
        second = service.export()
        assert first.parent == second.parent == service.export_root
        assert service.current_folder == service._store.folder
        assert not (service.current_folder / "exports").exists()
    finally:
        service._store.close()


def test_name_collision_does_not_overwrite_a_completed_export(tmp_path, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 23, 12, tzinfo=UTC)

    store = make_store(tmp_path / "task")
    try:
        monkeypatch.setattr(store_module, "datetime", Frozen)
        monkeypatch.setattr(store_module.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
        output = store.export(tmp_path / "exports")
        before = files_hashes(output)
        with pytest.raises(InspectionError, match="导出未完成"):
            store.export(tmp_path / "exports")
        assert files_hashes(output) == before
        assert not store.db.in_transaction
    finally:
        store.close()


def test_empty_unprepared_task_exports_without_claiming_a_group(tmp_path):
    store = Store(tmp_path / "task", create=True)
    try:
        output = store.export(tmp_path / "exports")
        assert "未封存任务" in output.name
        assert read_csv(output / "report.csv") == []
        report = json.loads((output / "report.json").read_text("utf-8"))
        assert report["groups"] == []
        assert report["metadata"]["prepared"] is False
        assert "尚未保存群快照" in store.task_label()
    finally:
        store.close()


def test_failed_completion_marker_write_does_not_claim_success(tmp_path, monkeypatch):
    store = make_store(tmp_path / "task")
    original = Path.write_text

    def fail_marker(path, data, **kwargs):
        result = original(path, data, **kwargs)
        if path.name == ".complete.tmp":
            raise OSError("synthetic marker flush failure")
        return result

    try:
        monkeypatch.setattr(Path, "write_text", fail_marker)
        with pytest.raises(InspectionError, match="导出未完成"):
            store.export(tmp_path / "exports")
        assert not list((tmp_path / "exports").rglob("COMPLETE"))
        assert store.pending() == [MEMBER, OTHER]
        assert not store.db.in_transaction
    finally:
        store.close()
