"""Synthetic WAL, encrypted evidence, and isolated recovery; never production data."""

import hashlib
import os
import sqlite3
from dataclasses import replace

import pytest
from app.reports.backup import backup_sqlite
from app.reports.backup_crypto import seal_file, unseal_file
from app.reports.backup_paths import private_directory
from app.reports.backup_store import BackupConfig, BackupError, restore_snapshot, run_backup
from cryptography.exceptions import InvalidTag


@pytest.fixture
def config(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data/media").mkdir(parents=True)
    (repo / "data/sample_pool").mkdir()
    (repo / "config").mkdir()
    (repo / ".env").write_text("API_KEY=synthetic-not-a-real-secret\n")
    (repo / "config/ai_prompt_rules.txt").write_text("synthetic rules")
    (repo / "data/media/photo.jpg").write_bytes(b"synthetic-media" * 123)
    (repo / "data/media/document.bin").write_bytes(b"completed-generic-attachment")
    (repo / "data/sample_pool/evidence.json").write_text('{"synthetic":true}')
    con = sqlite3.connect(repo / "data/moderation.db")
    con.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA wal_autocheckpoint=0;
    CREATE TABLE alembic_version(version_num TEXT);
    INSERT INTO alembic_version VALUES('e1c7d4b8a902');
    CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT);
    INSERT INTO system_settings VALUES('image_decision_authority_version','1');
    INSERT INTO system_settings VALUES('runtime_emergency_stop','false');
    CREATE TABLE image_allowlist(phash TEXT,decision_state TEXT,decision_source TEXT,
      decision_operator TEXT,decision_version INTEGER,decided_at TEXT,history_json TEXT);
    CREATE TABLE cases(id INTEGER PRIMARY KEY, status TEXT);
    INSERT INTO cases VALUES(1,'PENDING_REVIEW');
    CREATE TABLE action_intents(id INTEGER PRIMARY KEY,status TEXT);
    INSERT INTO action_intents VALUES(1,'UNKNOWN');
    CREATE TABLE onebot_inbox(id INTEGER PRIMARY KEY,status TEXT);
    INSERT INTO onebot_inbox VALUES(1,'PROCESSING');
    CREATE TABLE provider_group_settings(id INTEGER);
    CREATE TABLE allowlist_members(id INTEGER);
    CREATE TABLE shadow_decisions(detail_json TEXT,created_at TEXT);
    CREATE TABLE violation_records(message_snapshot_json TEXT,created_at TEXT);
    """)
    con.commit()
    state = tmp_path / "state"
    private_directory(state)
    key_file = state / "backup.key"
    key_file.write_bytes(os.urandom(32))
    key_file.chmod(0o600)
    yield BackupConfig(repo, tmp_path / "backups", state, key_file, min_free_bytes=0)
    con.close()


def test_online_database_can_target_separate_disk_directory(config, tmp_path):
    backup = backup_sqlite(
        f"sqlite:///{config.repo / 'data/moderation.db'}", destination_dir=tmp_path / "other"
    )
    assert backup.parent == tmp_path / "other"
    with sqlite3.connect(backup) as con:
        assert con.execute("select status from cases").fetchall() == [("PENDING_REVIEW",)]
    assert not (config.repo / "data/backups").exists()


def test_authenticated_stream_roundtrip_and_wrong_key(tmp_path):
    source = tmp_path / "original"
    payload = b"sensitive-synthetic" * 100000
    source.write_bytes(payload)
    key = os.urandom(32)
    encrypted = tmp_path / "sealed"
    expected = hashlib.sha256(payload).hexdigest(), len(payload)
    assert seal_file(source, encrypted, key, b"context") == expected
    assert b"sensitive-synthetic" not in encrypted.read_bytes()
    restored = tmp_path / "restored"
    assert unseal_file(encrypted, restored, key, b"context") == expected
    assert restored.read_bytes() == payload
    with pytest.raises(InvalidTag):
        unseal_file(encrypted, None, os.urandom(32), b"context")


@pytest.mark.parametrize("damage", ["flip", "truncate", "context"])
def test_modified_ciphertext_never_verifies(tmp_path, damage):
    source = tmp_path / "original"
    source.write_bytes(b"secret" * 200)
    encrypted = tmp_path / "sealed"
    key = os.urandom(32)
    seal_file(source, encrypted, key, b"identity")
    raw = bytearray(encrypted.read_bytes())
    if damage == "flip":
        raw[50] ^= 1
    elif damage == "truncate":
        raw = raw[:-1]
    encrypted.write_bytes(raw)
    with pytest.raises(InvalidTag):
        unseal_file(encrypted, None, key, b"wrong" if damage == "context" else b"identity")


def test_complete_backup_restores_wal_media_and_config_without_starting_runtime(config, tmp_path):
    before = (config.repo / ".env").read_bytes()
    result = run_backup(config, source_sha="a" * 40)
    assert result["status"] == "success"
    recovered = tmp_path / "recovered"
    verification = restore_snapshot(config, result["snapshot_id"], recovered)
    assert verification["status"] == "verified"
    assert (recovered / ".env").read_bytes() == before
    assert (recovered / "data/media/document.bin").read_bytes() == b"completed-generic-attachment"
    assert (recovered / "data/media/photo.jpg").read_bytes() == (
        config.repo / "data/media/photo.jpg"
    ).read_bytes()
    with sqlite3.connect(recovered / "data/moderation.db") as c:
        assert c.execute("select status from action_intents").fetchone()[0] == "UNKNOWN"
        assert c.execute("select status from onebot_inbox").fetchone()[0] == "PROCESSING"
    assert (config.repo / ".env").read_bytes() == before
    assert not list(config.destination.rglob("*.db"))
    assert not list(config.destination.rglob(".env"))


def test_repeat_backup_reuses_immutable_media_objects(config):
    first = run_backup(config, source_sha="a" * 40)
    objects = {p.name: p.read_bytes() for p in (config.destination / "objects").iterdir()}
    second = run_backup(config, source_sha="a" * 40)
    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["reused_objects"] >= 4
    for name, data in objects.items():
        assert (config.destination / "objects" / name).read_bytes() == data


def test_out_of_space_does_not_publish_success_or_delete_old_backups(config):
    good = run_backup(config, source_sha="a" * 40)
    previous = (config.destination / "last_success.json").read_bytes()
    with pytest.raises(BackupError, match="BACKUP_QUOTA"):
        run_backup(replace(config, max_bytes=1), source_sha="a" * 40)
    assert (config.destination / "last_success.json").read_bytes() == previous
    assert (config.destination / "snapshots" / (good["snapshot_id"] + ".enc")).exists()


def test_restore_refuses_production_or_existing_output(config):
    result = run_backup(config, source_sha="a" * 40)
    with pytest.raises(BackupError, match="RESTORE_TARGET_NOT_FRESH"):
        restore_snapshot(config, result["snapshot_id"], config.repo)
    assert (config.repo / ".env").read_text().startswith("API_KEY=synthetic")


def test_unknown_schema_does_not_publish_completed_backup(config):
    with pytest.raises(BackupError, match="DATABASE_REVISION"):
        run_backup(
            replace(config, expected_revision="not-the-current-revision"), source_sha="a" * 40
        )
    assert not (config.destination / "last_success.json").exists()


def test_wrong_recovery_key_cannot_silently_start_a_new_store(config):
    run_backup(config, source_sha="a" * 40)
    previous = (config.destination / "last_success.json").read_bytes()
    config.key_file.write_bytes(os.urandom(32))
    with pytest.raises(BackupError, match="BACKUP_KEY_MISMATCH"):
        run_backup(config, source_sha="a" * 40)
    assert (config.destination / "last_success.json").read_bytes() == previous


def test_concurrent_backup_refused_and_lock_releases(config):
    from app.reports.backup_store import backup_lock

    run_backup(config, source_sha="a" * 40)
    with (
        backup_lock(config.destination),
        pytest.raises(BackupError, match="BACKUP_ALREADY_RUNNING"),
    ):
        run_backup(config, source_sha="a" * 40)
    assert run_backup(config, source_sha="a" * 40)["status"] == "success"


def test_corrupted_object_does_not_publish_restore_success(config, tmp_path):
    result = run_backup(config, source_sha="a" * 40)
    obj = next((config.destination / "objects").iterdir())
    data = bytearray(obj.read_bytes())
    data[-1] ^= 1
    obj.write_bytes(data)
    target = tmp_path / "bad-restore"
    with pytest.raises(BackupError, match="RESTORE_FAILED"):
        restore_snapshot(config, result["snapshot_id"], target)
    assert not (target / "RESTORE_VERIFIED.json").exists()
    assert (target / "RESTORE_FAILED.json").exists()


def test_failure_after_old_success_redacts_exception_and_preserves_snapshot(config, monkeypatch):
    import json

    import app.reports.backup_store as store

    run_backup(config, source_sha="a" * 40)
    previous = (config.destination / "last_success.json").read_bytes()
    (config.repo / "data/media/new.jpg").write_bytes(b"new-synthetic-image")

    def fail(*args, **kwargs):
        raise OSError("synthetic-private-content-must-not-leak")

    monkeypatch.setattr(store, "seal_file", fail)
    with pytest.raises(BackupError, match="BACKUP_FAILED"):
        run_backup(config, source_sha="a" * 40)
    assert (config.destination / "last_success.json").read_bytes() == previous
    receipts = [json.loads(p.read_text()) for p in (config.destination / "attempts").iterdir()]
    assert any(r["status"] == "failed" for r in receipts)
    assert "synthetic-private-content" not in str(receipts)


def test_changed_source_rejected(config, monkeypatch):
    import app.reports.backup_store as store

    original = store.seal_file

    def change(source, *args, **kwargs):
        if source.name == "photo.jpg":
            source.write_bytes(b"modified-during-capture")
        return original(source, *args, **kwargs)

    monkeypatch.setattr(store, "seal_file", change)
    with pytest.raises(BackupError, match="SOURCE_CHANGED"):
        run_backup(config, source_sha="a" * 40)
    assert not (config.destination / "last_success.json").exists()


def test_missing_historical_media_reported_not_invented(config):
    with sqlite3.connect(config.repo / "data/moderation.db") as con:
        con.execute(
            "INSERT INTO shadow_decisions VALUES(?,?)",
            (
                '{"media_files":[{"name":"missing.jpg"},{"name":"photo.jpg"}]}',
                "2026-09-22T00:00:00+00:00",
            ),
        )
    result = run_backup(config, source_sha="a" * 40)
    assert result["missing_media_count"] == 1
    assert result["invalid_media_references"] == 0


def test_uncommitted_rows_not_included_in_online_backup(config, tmp_path):
    writer = sqlite3.connect(config.repo / "data/moderation.db")
    writer.execute("INSERT INTO cases VALUES(2,'UNCOMMITTED')")
    try:
        result = run_backup(config, source_sha="a" * 40)
        target = tmp_path / "restore-committed"
        restore_snapshot(config, result["snapshot_id"], target)
        with sqlite3.connect(target / "data/moderation.db") as con:
            assert con.execute("SELECT count(*) FROM cases").fetchone() == (1,)
    finally:
        writer.rollback()
        writer.close()


def test_staging_disk_space_checked(config, monkeypatch):
    import app.reports.backup_store as store

    real = store.shutil.disk_usage

    def usage(path):
        r = real(path)
        if str(path).startswith(str(config.state_dir)):
            return type(r)(r.total, r.used, 1)
        return r

    monkeypatch.setattr(store.shutil, "disk_usage", usage)
    with pytest.raises(BackupError, match="STAGING_DISK_FREE"):
        run_backup(config, source_sha="a" * 40)
    assert not (config.destination / "last_success.json").exists()


def test_stream_crypto_checks_deadline(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"x" * 100)

    def expired():
        raise TimeoutError("expired")

    with pytest.raises(TimeoutError, match="expired"):
        seal_file(source, tmp_path / "target", os.urandom(32), b"test", check=expired)


def test_parent_and_symlink_paths_refused(config, tmp_path):
    from app.reports.backup_paths import checked

    with pytest.raises(ValueError, match="PARENT_PATH_REFUSED"):
        checked(tmp_path / "unknown/../repo")
    link = tmp_path / "linked-media"
    try:
        link.symlink_to(config.repo / "data/media", target_is_directory=True)
    except OSError:
        pytest.skip("symlink privilege unavailable; Windows junction tested separately")
    with pytest.raises(ValueError, match="LINK_PATH_REFUSED"):
        checked(link / "photo.jpg")


def test_windows_junction_parent_refused(config, tmp_path):
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("Windows junction")
    from app.reports.backup_paths import checked

    link = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(config.repo / "data/media")],
        capture_output=True,
    )
    assert result.returncode == 0
    with pytest.raises(ValueError, match="LINK_PATH_REFUSED"):
        checked(link / "photo.jpg")
    link.rmdir()  # Only the synthetic junction, never its target.


def test_cli_redacts_settings_failure(config, monkeypatch, capsys, tmp_path):
    import app.reports.scheduled_backup as cli

    monkeypatch.setattr(cli, "read_config", lambda path: config)

    def fail(*args):
        raise ValueError("synthetic-secret-settings")

    monkeypatch.setattr(cli, "validate_source", fail)
    assert cli.main(["run", "--config", str(tmp_path / "unused")]) == 1
    output = capsys.readouterr().out
    assert "synthetic-secret-settings" not in output
    assert "BACKUP_COMMAND_FAILED" in output


def test_initialize_persists_windows_owner_and_private_config(tmp_path):
    import sys

    from app.reports.scheduled_backup import initialize, read_config

    repo = tmp_path / "init-repo"
    (repo / ".git").mkdir(parents=True)
    path = initialize(
        repo, tmp_path / "init-backup", tmp_path / "init-state", None, mode="encrypted"
    )
    result = read_config(path)
    assert len(result.key_file.read_bytes()) == 32
    if sys.platform == "win32":
        assert result.owner_sid.startswith("S-1-")
    with pytest.raises(BackupError, match="INITIALIZE_REQUIRES_NEW_ROOTS"):
        initialize(repo, tmp_path / "init-backup", tmp_path / "init-state", result.owner_sid)


@pytest.mark.parametrize(
    "name", ["../outside", "data/media/CON.jpg", "data/media/bad. ", "unlisted/secret"]
)
def test_restore_rejects_authenticated_but_unsafe_manifest(config, tmp_path, name):
    import hashlib
    import json

    result = run_backup(config, source_sha="a" * 40)
    sid = result["snapshot_id"]
    sealed = config.destination / "snapshots" / (sid + ".enc")
    plain = tmp_path / "manifest"
    key = config.key_file.read_bytes()
    unseal_file(sealed, plain, key, ("manifest:" + sid).encode())
    data = json.loads(plain.read_text())
    data["files"][0]["path"] = name
    plain.write_text(json.dumps(data))
    altered = tmp_path / "altered"
    seal_file(plain, altered, key, ("manifest:" + sid).encode())
    altered.replace(sealed)
    # Preserve the outer receipt binding so this probes the independent path guard.
    receipt_path = config.destination / "attempts" / (sid + ".json")
    receipt = json.loads(receipt_path.read_text())
    receipt["manifest_sha256"] = hashlib.sha256(sealed.read_bytes()).hexdigest()
    receipt["manifest_size"] = sealed.stat().st_size
    receipt_path.write_text(json.dumps(receipt))
    target = tmp_path / "unsafe-restore"
    with pytest.raises(BackupError, match="RESTORE_FAILED"):
        restore_snapshot(config, sid, target)
    assert not (target / "RESTORE_VERIFIED.json").exists()
    assert not (tmp_path / "outside").exists()


def test_cleanup_query_errors_are_not_treated_as_idle(monkeypatch):
    from types import SimpleNamespace

    import app.reports.scheduled_backup as cli

    if cli.sys.platform != "win32":
        pytest.skip("Windows task query")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(BackupError, match="CLEANUP_STATE_UNKNOWN"):
        cli.cleanup_running()


def test_plain_backup_needs_no_key_and_omits_service_credentials(config, tmp_path):
    plain = replace(config, mode="plain", key_file=config.state_dir / "unused.key")
    (plain.repo / ".env").write_text(
        "ADMIN_PASSWORD=synthetic-admin-secret\n"
        "ONEBOT_ACCESS_TOKEN=synthetic-onebot-secret\n"
        "UNKNOWN_FUTURE_KEY=synthetic-unknown-secret\n"
        "AI_BASE_URL=https://example.test/?key=synthetic-url-secret\n"
        "NOTIFICATION_HEARTBEAT_URL=https://example.test/private-capability\n"
        "WEB_PORT=8001\nMEDIA_QUOTA_BYTES=21474836480\n",
        encoding="utf-8",
    )
    (plain.repo / "config/unrecognized-credentials.json").write_text("synthetic-config-secret")
    result = run_backup(plain, source_sha="a" * 40)
    assert result["protection"] == "plain"
    assert result["credentials_omitted"] is True
    assert not plain.key_file.exists()
    target = tmp_path / "plain-restore"
    restore_snapshot(plain, result["snapshot_id"], target)
    assert not (target / ".env").exists()
    template = (target / ".env.recovery-template").read_text()
    assert "WEB_PORT" in template and "8001" in template
    assert "MEDIA_QUOTA_BYTES" in template
    assert not (target / "config/unrecognized-credentials.json").exists()
    assert (target / "data/media/document.bin").read_bytes() == b"completed-generic-attachment"
    all_bytes = b"".join(p.read_bytes() for p in plain.destination.rglob("*") if p.is_file())
    for value in [
        b"synthetic-admin-secret",
        b"synthetic-onebot-secret",
        b"synthetic-unknown-secret",
        b"synthetic-url-secret",
        b"private-capability",
        b"synthetic-config-secret",
    ]:
        assert value not in all_bytes
    assert b"completed-generic-attachment" in all_bytes


def test_default_initialize_is_keyless_plain_backup(tmp_path):
    from app.reports.scheduled_backup import initialize, read_config

    repo = tmp_path / "plain-init-repo"
    (repo / ".git").mkdir(parents=True)
    config = read_config(
        initialize(repo, tmp_path / "plain-init-data", tmp_path / "plain-init-state", None)
    )
    assert config.mode == "plain"
    assert not config.key_file.exists()


def test_plain_store_rejects_old_encrypted_identity(config):
    run_backup(config, source_sha="a" * 40)
    previous = (config.destination / "last_success.json").read_bytes()
    with pytest.raises(BackupError, match="BACKUP_KEY_MISMATCH"):
        run_backup(replace(config, mode="plain"), source_sha="a" * 40)
    assert (config.destination / "last_success.json").read_bytes() == previous


def test_plain_corruption_prevents_restore_success(config, tmp_path):
    plain = replace(config, mode="plain")
    result = run_backup(plain, source_sha="a" * 40)
    obj = next((plain.destination / "objects").glob("*.blob"))
    obj.write_bytes(obj.read_bytes() + b"corrupt")
    target = tmp_path / "corrupt-plain-restore"
    with pytest.raises(BackupError, match="RESTORE_FAILED"):
        restore_snapshot(plain, result["snapshot_id"], target)
    assert not (target / "RESTORE_VERIFIED.json").exists()


@pytest.mark.parametrize(
    ("filename", "error"),
    [
        (".env", "ARCHIVE_CONTAINS_CREDENTIAL_FILE"),
        ("Config/credentials.json", "ARCHIVE_UNREVIEWED_CONFIG"),
    ],
)
def test_tracked_credentials_never_enter_plain_archive(config, filename, error):
    import subprocess

    plain = replace(config, mode="plain")

    def git(*args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Backup Test",
                "-c",
                "user.email=backup@example.invalid",
                *args,
            ],
            cwd=plain.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q")
    credential = plain.repo / filename
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("synthetic-credential")
    git("add", "--", filename)
    git("commit", "-qm", "synthetic accidentally tracked credential fixture")
    sha = git("rev-parse", "HEAD")
    with pytest.raises(BackupError, match=error):
        run_backup(plain, source_sha=sha)
    assert not (plain.destination / "last_success.json").exists()
    assert not list((plain.destination / "objects").iterdir())


def test_plain_valid_json_manifest_loss_detected(config, tmp_path):
    import json

    plain = replace(config, mode="plain")
    result = run_backup(plain, source_sha="a" * 40)
    manifest = plain.destination / "snapshots" / (result["snapshot_id"] + ".json")
    data = json.loads(manifest.read_text())
    data["files"] = [entry for entry in data["files"] if entry["path"] != "data/media/photo.jpg"]
    manifest.write_text(json.dumps(data))
    target = tmp_path / "lost-entry-restore"
    with pytest.raises(BackupError, match="MANIFEST_RECEIPT_MISMATCH"):
        restore_snapshot(plain, result["snapshot_id"], target)
    assert not (target / "RESTORE_VERIFIED.json").exists()


def test_plain_manifest_write_damage_does_not_publish_success(config, monkeypatch):
    import json

    import app.reports.backup_store as store

    plain = replace(config, mode="plain")
    original = store._write_object

    def damage_manifest(source, target, key, context, **kwargs):
        result = original(source, target, key, context, **kwargs)
        if context.startswith(b"manifest:"):
            data = json.loads(target.read_text())
            data["files"] = [
                entry for entry in data["files"] if entry["path"] != "data/media/photo.jpg"
            ]
            target.write_text(json.dumps(data))
        return result

    monkeypatch.setattr(store, "_write_object", damage_manifest)
    with pytest.raises(BackupError, match="MANIFEST_WRITE_MISMATCH"):
        run_backup(plain, source_sha="a" * 40)
    assert not (plain.destination / "last_success.json").exists()
