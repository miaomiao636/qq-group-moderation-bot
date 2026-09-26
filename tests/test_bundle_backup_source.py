"""Company delivery recovery uses synthetic sources and never the deployed database."""

import hashlib
import json
import zipfile
from dataclasses import replace

import pytest
from app.reports import scheduled_backup
from app.reports.backup_store import restore_snapshot, run_backup

from tests.test_scheduled_backup import config as config


def delivery(root):
    files = {
        "app/__init__.py": b"# synthetic application\n",
        "alembic/versions/synthetic.py": b"revision = 'synthetic'\n",
        "config/ai_prompt_rules.txt": b"synthetic rules",
        ".env.example": b"ADMIN_PASSWORD=\n",
        "pyproject.toml": b"# synthetic dependency metadata\n",
        "uv.lock": b"# synthetic lock\n",
        "alembic.ini": b"[alembic]\n",
        "LICENSE": b"MIT\n",
        "DELIVERY.md": b"Synthetic delivery\n",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {
        "format": 1,
        "source_sha": "a" * 40,
        "files": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in sorted(files.items())
        ],
    }
    raw = json.dumps(manifest).encode()
    (root / "DELIVERY-MANIFEST.json").write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), files


def test_backup_initializes_without_git_but_pins_delivery(tmp_path):
    root = tmp_path / "delivery"
    root.mkdir()
    digest, _ = delivery(root)
    path = scheduled_backup.initialize(
        root,
        tmp_path / "store",
        tmp_path / "state",
        None,
        source_kind="bundle",
        manifest_sha256=digest,
    )
    saved = scheduled_backup.read_config(path)
    assert saved.source_kind == "bundle"
    assert saved.source_manifest_sha256 == digest
    assert not (root / ".git").exists()


def test_bundle_wal_backup_restores_verified_source_and_data(config, tmp_path):
    digest, files = delivery(config.repo)
    cfg = replace(config, mode="plain", source_kind="bundle", source_manifest_sha256=digest)
    receipt = run_backup(cfg, source_sha="a" * 40)
    target = tmp_path / "restored"
    assert restore_snapshot(cfg, receipt["snapshot_id"], target)["status"] == "verified"
    with zipfile.ZipFile(target / "recovery-source.zip") as archive:
        assert set(archive.namelist()) == {*files, "DELIVERY-MANIFEST.json"}
        for name, content in files.items():
            assert archive.read(name) == content
    assert not (target / ".env").exists()
    assert (target / "data/media/photo.jpg").read_bytes() == (
        config.repo / "data/media/photo.jpg"
    ).read_bytes()


@pytest.mark.parametrize("damage", ["hash", "missing", "extra", "root_module", "manifest", "git"])
def test_changed_delivery_cannot_backup(config, damage):
    digest, _ = delivery(config.repo)
    cfg = replace(config, mode="plain", source_kind="bundle", source_manifest_sha256=digest)
    if damage == "hash":
        (config.repo / "app/__init__.py").write_bytes(b"changed")
    elif damage == "missing":
        (config.repo / "uv.lock").unlink()
    elif damage == "extra":
        (config.repo / "app/extra.py").write_bytes(b"# unlisted")
    elif damage == "root_module":
        (config.repo / "sitecustomize.py").write_bytes(b"# unlisted")
    elif damage == "manifest":
        (config.repo / "DELIVERY-MANIFEST.json").write_bytes(b"{}")
    else:
        (config.repo / ".git").mkdir()
    with pytest.raises((ValueError, RuntimeError)):
        run_backup(cfg, source_sha="a" * 40)
    assert not (cfg.destination / "last_success.json").exists()


def test_source_archive_rejects_inner_tamper(tmp_path):
    from app.reports.backup_source import archive_bundle, verify_archive

    root = tmp_path / "source"
    root.mkdir()
    digest, files = delivery(root)
    archive = tmp_path / "source.zip"
    archive_bundle(root, digest, "a" * 40, archive)
    assert verify_archive(archive, digest, "a" * 40) is None
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as output, zipfile.ZipFile(archive) as original:
        for item in original.infolist():
            output.writestr(
                item, b"changed" if item.filename == "app/__init__.py" else original.read(item)
            )
    with pytest.raises(ValueError):
        verify_archive(bad, digest, "a" * 40)
    assert files["app/__init__.py"] == (root / "app/__init__.py").read_bytes()


def test_source_archive_is_deterministic(tmp_path):
    from app.reports.backup_source import archive_bundle

    root = tmp_path / "source"
    root.mkdir()
    digest, _ = delivery(root)
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    archive_bundle(root, digest, "a" * 40, first)
    archive_bundle(root, digest, "a" * 40, second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in archive.infolist())


def test_validate_source_uses_pinned_bundle_and_preserves_schema_guard(config, monkeypatch):
    from app import config as app_config
    from app.reports.backup_store import BackupError

    digest, _ = delivery(config.repo)
    cfg = replace(config, source_kind="bundle", source_manifest_sha256=digest)
    monkeypatch.setattr(
        scheduled_backup, "__file__", str(config.repo / "app/reports/scheduled_backup.py")
    )
    settings = app_config.Settings(
        _env_file=None,
        DATABASE_URL=f"sqlite:///{config.repo / 'data/moderation.db'}",
        AI_PROMPT_RULES_FILE=str(config.repo / "config/ai_prompt_rules.txt"),
    )
    monkeypatch.setattr(app_config, "Settings", lambda **kwargs: settings)
    monkeypatch.setattr(scheduled_backup, "_current_revision", lambda: cfg.expected_revision)
    monkeypatch.setattr(
        scheduled_backup, "_git", lambda *args: pytest.fail("bundle must not use git")
    )
    assert scheduled_backup.validate_source(cfg) == "a" * 40
    monkeypatch.setattr(scheduled_backup, "_current_revision", lambda: "different")
    with pytest.raises(BackupError, match="SOURCE_SCHEMA_MISMATCH"):
        scheduled_backup.validate_source(cfg)
