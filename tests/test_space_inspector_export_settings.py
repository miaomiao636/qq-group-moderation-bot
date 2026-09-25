"""Synthetic local export preferences; no QQ or production access."""

import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from app.space_inspector import export_paths
from app.space_inspector.contracts import InspectionError
from app.space_inspector.service import Service
from app.space_inspector.worker import run_worker

from tests.test_space_inspector_exports import files_hashes
from tests.test_space_inspector_store import make_store


def service_at(root):
    service = Service.__new__(Service)
    service.root = root
    root.mkdir(exist_ok=True)
    service.load_export_settings()
    return service


def test_selection_survives_restart_and_preserves_existing_results(tmp_path, monkeypatch):
    default = tmp_path / "desktop"
    monkeypatch.setattr(export_paths, "default_export_root", lambda: default)
    service = service_at(tmp_path / "data")
    service._store = make_store(service.root / "tasks" / "20260925T120000Z-0123abcd")
    service.current_folder = service._store.folder
    custom = tmp_path / "自选结果目录"
    custom.mkdir()
    try:
        old = service.export()
        before = files_hashes(old)
        service.set_export_root(custom)
        new = service.export()
        assert new.parent == custom
        assert files_hashes(old) == before
        assert service.current_folder == service._store.folder
        assert service_at(service.root).export_root == custom
    finally:
        service._store.close()


@pytest.mark.parametrize("target", ["relative", "missing", "file", "internal"])
def test_invalid_selection_keeps_previous_preference(tmp_path, target):
    service = service_at(tmp_path / "data")
    valid = tmp_path / "valid"
    valid.mkdir()
    service.set_export_root(valid)
    choices = {
        "relative": Path("relative"),
        "missing": tmp_path / "missing",
        "file": tmp_path / "file",
        "internal": service.root / "tasks",
    }
    choices["file"].write_text("keep", encoding="utf-8")
    choices["internal"].mkdir()
    with pytest.raises(InspectionError):
        service.set_export_root(choices[target])
    assert service.export_root == valid
    assert service_at(service.root).export_root == valid


def test_settings_replace_failure_keeps_live_and_saved_preference(tmp_path, monkeypatch):
    service = service_at(tmp_path / "data")
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    service.set_export_root(old)

    def fail_replace(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(export_paths.os, "replace", fail_replace)
    with pytest.raises(InspectionError):
        service.set_export_root(new)
    assert service.export_root == old
    assert service_at(service.root).export_root == old
    assert list(new.iterdir()) == []
    assert not list(service.root.glob("*.tmp"))


@pytest.mark.parametrize("contents", ['{"export_root": "relative"}', "broken json", "[]"])
def test_corrupt_preference_blocks_export_until_reselected(tmp_path, contents):
    root = tmp_path / "data"
    root.mkdir()
    (root / "export-settings.json").write_text(contents, encoding="utf-8")
    service = service_at(root)
    service._store = make_store(root / "task")
    try:
        assert service.export_settings_error
        with pytest.raises(InspectionError, match="选择导出位置"):
            service.export()
        chosen = tmp_path / "chosen"
        chosen.mkdir()
        service.set_export_root(chosen)
        assert not service.export_settings_error
        assert service.export().parent == chosen
    finally:
        service._store.close()


def test_saved_missing_destination_does_not_fall_back_or_recreate(tmp_path):
    service = service_at(tmp_path / "data")
    custom = tmp_path / "removable"
    custom.mkdir()
    service.set_export_root(custom)
    custom.rmdir()
    restarted = service_at(service.root)
    restarted._store = make_store(service.root / "task")
    try:
        assert restarted.export_root == custom
        with pytest.raises(InspectionError):
            restarted.export()
        assert not custom.exists()
    finally:
        restarted._store.close()


def test_unwritable_selection_keeps_preference(tmp_path, monkeypatch):
    service = service_at(tmp_path / "data")
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    service.set_export_root(old)

    def denied(*args, **kwargs):
        raise PermissionError("synthetic directory permission")

    monkeypatch.setattr(export_paths.tempfile, "TemporaryFile", denied)
    with pytest.raises(InspectionError):
        service.set_export_root(new)
    assert service.export_root == service_at(service.root).export_root == old


def test_worker_reports_saved_location_even_when_group_directory_is_offline(tmp_path):
    from tests.test_space_inspector_gui import FakeService

    class LocalService(FakeService):
        def groups(self):
            raise InspectionError("synthetic offline")

        def set_export_root(self, chosen):
            self.record("set_export_root")
            self.export_root = chosen

    commands, events = queue.Queue(), queue.Queue()
    chosen = tmp_path / "chosen"
    for command in [("groups", None), ("set_export_root", chosen), ("close", None)]:
        commands.put(command)
    fake = LocalService([])
    run_worker(commands, events, threading.Event(), lambda: cast(Service, fake))
    first, saved, closed = [events.get_nowait() for _ in range(3)]
    assert first[0] == "error"
    assert first[1]["export_root"] == str(FakeService.export_root)
    assert saved == (
        "export_location_saved",
        {"export_root": str(chosen), "export_settings_error": ""},
    )
    assert closed[0] == "closed"
    assert any(action == "set_export_root" for action, _ in fake.calls)


def test_directory_chooser_cancel_and_busy_do_not_submit(tmp_path, monkeypatch):
    pytest.importorskip("tkinter")
    from app.space_inspector import gui

    window = gui.Window.__new__(gui.Window)
    window.root = None
    window._busy = window._closing = window._shutdown_failed = False
    window._export_root_text = SimpleNamespace(get=lambda: str(tmp_path))
    submitted = []
    window._submit = lambda *args: submitted.append(args)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kwargs: "")
    window._choose_export_location()
    assert submitted == []
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kwargs: str(tmp_path))
    window._choose_export_location()
    assert submitted == [("set_export_root", tmp_path)]
    window._busy = True
    window._choose_export_location()
    assert len(submitted) == 1
