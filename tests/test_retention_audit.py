"""Read-only N03 inventory; all databases, names and contents are synthetic."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retention_audit.py"
SPEC = importlib.util.spec_from_file_location("retention_audit_candidate", SCRIPT)
assert SPEC and SPEC.loader
audit_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_module
SPEC.loader.exec_module(audit_module)

AS_OF = "2026-09-22T00:00:00+00:00"
OLD = "2026-09-01T00:00:00+00:00"
RECENT = "2026-09-21T00:00:00+00:00"
PRIVATE = "SYNTHETIC_RAW_NOT_FOR_REPORT_912340876"


def make_db(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    db = root / "moderation.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            "CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, detail_json TEXT, "
            "created_at TEXT, external_user_id TEXT, reason TEXT);"
            "CREATE TABLE violation_records (id INTEGER PRIMARY KEY, "
            "message_snapshot_json TEXT, created_at TEXT, external_user_id TEXT, reason TEXT);"
        )
    return db


def add_source(db: Path, name: str, sent_at: object, *, table: str = "shadow_decisions") -> None:
    if table == "shadow_decisions":
        column, files = "detail_json", {"media_files": [{"name": name}]}
    else:
        column, files = "message_snapshot_json", {"attachments": [{"filename": name}]}
    payload = {"sent_at": sent_at, "text_preview": PRIVATE, "text": PRIVATE, **files}
    with sqlite3.connect(db) as connection:
        connection.execute(
            f"INSERT INTO {table} ({column},created_at,external_user_id,reason) VALUES (?,?,?,?)",
            (json.dumps(payload), RECENT, "912340876", PRIVATE),
        )


def make_file(data: Path, relative: str) -> Path:
    result = data / relative
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_bytes(PRIVATE.encode())
    return result


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_recent_file_timestamp_cannot_renew_old_source(tmp_path: Path) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    make_file(data, "media/912340876.jpg")
    add_source(db, "912340876.jpg", OLD)
    before = snapshot(data)

    result = audit_module.audit(db, data, as_of=AS_OF)

    assert snapshot(data) == before
    item = result["files"][0]
    assert item["status"] == "known_source_expired_review_required"
    assert item["expires_at"] == "2026-09-16T00:00:00+00:00"
    assert item["mtime_after_source"] is True
    assert item["safe_to_delete"] is False
    assert result["source_summary"]["expired_source_processed_recent"] == 1
    output = json.dumps(result)
    assert PRIVATE not in output
    assert "912340876" not in output
    assert str(data) not in output


def test_unknown_reference_prevents_complete_source_claim(tmp_path: Path) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    make_file(data, "media/shared.png")
    add_source(db, "shared.png", RECENT)
    add_source(db, "shared.png", OLD, table="violation_records")
    add_source(db, "shared.png", None)
    result = audit_module.audit(db, data, as_of=AS_OF)
    item = result["files"][0]
    assert item["oldest_known_source_at"] == OLD
    assert item["source_references"] == 3
    assert item["unknown_source_references"] == 1
    assert item["status"] == "source_time_unknown_review_required"
    assert item["expires_at"] is None
    assert item["safe_to_delete"] is False


@pytest.mark.parametrize(
    "stamp", ["invalid", "2026-09-01", 912340876, {"text": PRIVATE}, "2099-01-01T00:00:00Z"]
)
def test_invalid_and_future_time_is_unknown_without_echo(tmp_path: Path, stamp: object) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    make_file(data, "media/item.jpg")
    add_source(db, "item.jpg", stamp)
    result = audit_module.audit(db, data, as_of=AS_OF)
    assert result["files"][0]["status"] == "source_time_unknown_review_required"
    assert PRIVATE not in json.dumps(result)
    assert "912340876" not in json.dumps(result)


def test_explicit_scope_only_no_content_reads_and_no_mtime_expiry(
    tmp_path: Path, monkeypatch
) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    make_file(data, "backups/old.db")
    make_file(data, "sample_pool/912340876.jsonl")
    make_file(data, "unregistered/private.txt")
    make_file(data, "media/nested_unregistered/private.txt")
    make_file(data, "media/orphan.jpg")
    evidence = tmp_path / "evidence"
    make_file(evidence, "912340876.txt")
    before = snapshot(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("inventory must never read file contents")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", forbidden)
        patch.setattr(Path, "read_text", forbidden)
        result = audit_module.audit(db, data, as_of=AS_OF, evidence_roots=[evidence])
    assert snapshot(tmp_path) == before
    assert {item["scope"] for item in result["files"]} == {
        "media",
        "backups",
        "sample_pool",
        "external_evidence_1",
    }
    assert len(result["files"]) == 4
    assert all(item["expires_at"] is None for item in result["files"])
    assert all(item["safe_to_delete"] is False for item in result["files"])
    assert PRIVATE not in json.dumps(result) and "912340876" not in json.dumps(result)


def test_output_is_exclusive_and_never_inside_audited_tree(tmp_path: Path) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    output = tmp_path / "report.json"
    arguments = ["--db", str(db), "--data-root", str(data), "--as-of", AS_OF, "--out", str(output)]
    assert audit_module.main(arguments) == 0
    original = output.read_bytes()
    assert audit_module.main(arguments) == 1
    assert output.read_bytes() == original
    arguments[-1] = str(data / "report.json")
    assert audit_module.main(arguments) == 1
    assert not (data / "report.json").exists()


def test_scan_limit_fails_instead_of_claiming_complete(tmp_path: Path) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    for index in range(8):
        make_file(data, f"media/{index}.jpg")
    before = snapshot(data)
    with pytest.raises(audit_module.AuditError, match="entry_limit_exceeded"):
        audit_module.audit(db, data, as_of=AS_OF, max_entries=3)
    assert snapshot(data) == before


def test_source_limit_fails_instead_of_omitting_unknown_sources(tmp_path: Path) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    make_file(data, "media/item.jpg")
    add_source(db, "item.jpg", OLD)
    add_source(db, "item.jpg", None)
    with pytest.raises(audit_module.AuditError, match="source_limit_exceeded"):
        audit_module.audit(db, data, as_of=AS_OF, max_sources=1)


def make_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("host cannot create directory symlinks")
        # NTFS junctions exercise the production guard without requiring the
        # Windows symbolic-link privilege. Both paths are synthetic tmp_path.
        environment = dict(
            os.environ, RETENTION_TEST_LINK=str(link), RETENTION_TEST_TARGET=str(target)
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:RETENTION_TEST_LINK -Target $env:RETENTION_TEST_TARGET | Out-Null",
            ],
            capture_output=True,
            env=environment,
            timeout=15,
            check=False,
        )
        if result.returncode:
            pytest.skip("host cannot create directory links or junctions")


def test_linked_descendant_is_reported_but_never_followed(tmp_path: Path) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    outside = tmp_path / "outside"
    make_file(outside, "secret.jpg")
    make_link(data / "media", outside)
    result = audit_module.audit(db, data, as_of=AS_OF)
    assert result["files"] == []
    assert result["blocked_entries"][0]["reason"] == "link_or_unreadable"


def test_link_in_root_ancestor_fails_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    data = real / "nested" / "data"
    make_db(data)
    alias = tmp_path / "alias"
    make_link(alias, real)
    with pytest.raises(audit_module.AuditError, match="unsafe_input_path"):
        audit_module.audit(alias / "nested/data/moderation.db", alias / "nested/data", as_of=AS_OF)


def test_missing_database_is_not_created(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    db = data / "absent.db"
    with pytest.raises(audit_module.AuditError):
        audit_module.audit(db, data, as_of=AS_OF)
    assert not db.exists()


def test_malformed_payload_does_not_abort_or_echo_original(tmp_path: Path) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO shadow_decisions(detail_json,created_at) VALUES (?,?)", (PRIVATE, RECENT)
        )
    result = audit_module.audit(db, data, as_of=AS_OF)
    assert result["source_summary"]["invalid_payload"] == 1
    assert PRIVATE not in json.dumps(result)


def test_only_required_metadata_columns_are_selected(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    add_source(db, "item.jpg", OLD)
    real_connect = sqlite3.connect

    def guarded_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ and column in {"external_user_id", "reason"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(audit_module.sqlite3, "connect", guarded_connect)
    result = audit_module.audit(db, data, as_of=AS_OF)
    assert result["source_summary"]["sources"] == 1


def test_path_reference_cannot_escape_media_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    db = make_db(data)
    add_source(db, "../../912340876.jpg", OLD)
    result = audit_module.audit(db, data, as_of=AS_OF)
    assert result["source_summary"]["invalid_file_references"] == 1
    assert "912340876" not in json.dumps(result)
