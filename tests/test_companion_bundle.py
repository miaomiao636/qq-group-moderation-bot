"""Delivery archives use committed synthetic inputs, never the operator's live files."""

import hashlib
import json
import subprocess
import sys
import zipfile

import pytest


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def fixture_repo(tmp_path):
    from scripts import build_companion_bundle as bundle

    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Synthetic fixture")
    files = {name: "synthetic public material\n" for name in bundle.REQUIRED_FILES}
    files.update(
        {
            "app/__init__.py": 'NAME = "committed"\n',
            "app/space_inspector/__init__.py": "",
            "alembic/versions/synthetic.py": "revision = 'synthetic'\n",
            "docs/evidence/private.md": "EXCLUDED_HISTORY_SENTINEL",
            "tests/private_fixture.txt": "EXCLUDED_FIXTURE_SENTINEL",
        }
    )
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "synthetic source")
    return root, git(root, "rev-parse", "HEAD")


def test_archive_is_reproducible_from_commit_and_excludes_live_data(tmp_path):
    from scripts import build_companion_bundle as bundle

    root, sha = fixture_repo(tmp_path)
    (root / ".env").write_text("LOCAL_SECRET_SENTINEL", encoding="utf-8")
    (root / "app/__init__.py").write_text("DIRTY_WORKTREE_SENTINEL", encoding="utf-8")
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    receipt = bundle.build(root, "HEAD", first)
    bundle.build(root, sha, second)
    assert first.read_bytes() == second.read_bytes()
    assert receipt["source_sha"] == sha
    assert receipt["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    with zipfile.ZipFile(first) as archive:
        prefix = f"qqbot-companion-{sha[:12]}/"
        manifest = json.loads(archive.read(prefix + "DELIVERY-MANIFEST.json"))
        assert (
            receipt["manifest_sha256"]
            == hashlib.sha256(archive.read(prefix + "DELIVERY-MANIFEST.json")).hexdigest()
        )
        assert manifest["status"] == "candidate_not_accepted"
        assert manifest["source_sha"] == sha
        assert archive.read(prefix + "app/__init__.py") == b'NAME = "committed"\n'
        assert prefix + "config/ai_prompt_rules.txt" in archive.namelist()
        assert prefix + "scripts/retention_audit.py" in archive.namelist()
        assert not any("docs/evidence/" in name or "/tests/" in name for name in archive.namelist())
        for item in manifest["files"]:
            assert hashlib.sha256(archive.read(prefix + item["path"])).hexdigest() == item["sha256"]
        assert all(b"SENTINEL" not in archive.read(name) for name in archive.namelist())


def test_existing_bundle_is_never_overwritten(tmp_path):
    from scripts import build_companion_bundle as bundle

    root, sha = fixture_repo(tmp_path)
    output = tmp_path / "existing.zip"
    output.write_bytes(b"keep original")
    with pytest.raises(bundle.BundleError):
        bundle.build(root, sha, output)
    assert output.read_bytes() == b"keep original"


def test_missing_runtime_resource_aborts_without_archive(tmp_path):
    from scripts import build_companion_bundle as bundle

    root, _ = fixture_repo(tmp_path)
    git(root, "rm", "config/ai_prompt_rules.txt")
    git(root, "commit", "-qm", "missing resource")
    output = tmp_path / "missing.zip"
    with pytest.raises(bundle.BundleError, match="config/ai_prompt_rules.txt"):
        bundle.build(root, "HEAD", output)
    assert not output.exists()


def test_tracked_symlink_is_rejected_without_following_target(tmp_path):
    from scripts import build_companion_bundle as bundle

    root, _ = fixture_repo(tmp_path)
    oid = (
        subprocess.check_output(
            ["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=b"../../.env"
        )
        .decode()
        .strip()
    )
    git(root, "update-index", "--add", "--cacheinfo", f"120000,{oid},app/link.py")
    git(root, "commit", "-qm", "synthetic symlink")
    with pytest.raises(bundle.BundleError, match="link"):
        bundle.build(root, "HEAD", tmp_path / "link.zip")


def test_unreviewed_file_in_runtime_tree_is_rejected(tmp_path):
    from scripts import build_companion_bundle as bundle

    root, _ = fixture_repo(tmp_path)
    (root / "app/session.json").write_text("EXCLUDED_SESSION_SENTINEL", encoding="utf-8")
    git(root, "add", "app/session.json")
    git(root, "commit", "-qm", "unreviewed runtime file")
    with pytest.raises(bundle.BundleError):
        bundle.build(root, "HEAD", tmp_path / "session.zip")


def test_cli_build_receipt_identifies_source_and_digest(tmp_path):
    root, sha = fixture_repo(tmp_path)
    output = tmp_path / "cli.zip"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_companion_bundle.py",
            "--repo",
            str(root),
            "--ref",
            sha,
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["source_sha"] == sha
    assert receipt["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_nonempty_credential_in_public_template_blocks_packaging(tmp_path):
    from scripts import build_companion_bundle as bundle

    root, _ = fixture_repo(tmp_path)
    (root / ".env.example").write_text(
        "ONEBOT_ACCESS_TOKEN=synthetic-private-credential\n", encoding="utf-8"
    )
    git(root, "add", ".env.example")
    git(root, "commit", "-qm", "unsafe template")
    with pytest.raises(bundle.BundleError, match="template"):
        bundle.build(root, "HEAD", tmp_path / "unsafe.zip")
