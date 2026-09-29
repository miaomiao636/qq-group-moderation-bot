"""Explicit backup operations. No migrations, services, notifications or cleanup."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from app.reports.backup_paths import checked, private_directory, regular, verify_private
from app.reports.backup_store import BackupConfig, BackupError, _json, restore_snapshot, run_backup


def read_config(path: Path) -> BackupConfig:
    data = json.loads(regular(path).read_text(encoding="utf-8"))
    config = BackupConfig(
        **{
            **data,
            **{name: Path(data[name]) for name in ("repo", "destination", "state_dir", "key_file")},
        }
    )
    if path.absolute().parent != config.state_dir.absolute():
        raise BackupError("CONFIG_OUTSIDE_STATE")
    verify_private(path, config.owner_sid)
    return config


def initialize(
    repo: Path,
    destination: Path,
    state: Path,
    owner_sid: str | None,
    *,
    mode: str = "plain",
    source_kind: str = "git",
    manifest_sha256: str = "",
) -> Path:
    if mode not in {"plain", "encrypted"}:
        raise BackupError("UNKNOWN_BACKUP_MODE")
    if sys.platform == "win32" and owner_sid is None:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        owner_sid = result.stdout.strip()
    if sys.platform == "win32" and (
        not owner_sid or not re.fullmatch(r"S-1-\d+(?:-\d+)+", owner_sid)
    ):
        raise BackupError("INVALID_OWNER_SID")
    if source_kind == "git":
        checked(repo / ".git")
        if manifest_sha256:
            raise BackupError("UNEXPECTED_DELIVERY_PIN")
    elif source_kind == "bundle":
        from app.reports.backup_source import validate_bundle

        validate_bundle(repo, manifest_sha256)
    else:
        raise BackupError("UNKNOWN_SOURCE_KIND")
    for a, b in ((repo, destination), (repo, state), (destination, state)):
        a, b = a.absolute(), b.absolute()
        if a == b or a in b.parents or b in a.parents:
            raise BackupError("OVERLAPPING_ROOTS")
    if state.exists() or destination.exists():
        raise BackupError("INITIALIZE_REQUIRES_NEW_ROOTS")
    private_directory(state, owner_sid)
    private_directory(destination, owner_sid)
    key_file = state / "recovery.key"
    if mode == "encrypted":
        with key_file.open("xb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            key_file.chmod(0o600)
    config = BackupConfig(
        repo.absolute(),
        destination.absolute(),
        state.absolute(),
        key_file.absolute(),
        owner_sid=owner_sid,
        mode=mode,
        expected_revision=_current_revision(),
        source_kind=source_kind,
        source_manifest_sha256=manifest_sha256,
    )
    payload = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in asdict(config).items()
    }
    path = state / "config.json"
    _json(path, payload)
    return path


def _current_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if not head:
        raise BackupError("SOURCE_SCHEMA_UNKNOWN")
    return head


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo.as_posix()}", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return result.stdout.strip()


def validate_source(config: BackupConfig) -> str:
    if config.repo.absolute() != Path(__file__).absolute().parents[2]:
        raise BackupError("WRONG_SOURCE_CHECKOUT")
    if config.source_kind == "git":
        if _git(config.repo, "status", "--porcelain"):
            raise BackupError("SOURCE_NOT_CLEAN")
        source_sha = _git(config.repo, "rev-parse", "HEAD")
    elif config.source_kind == "bundle":
        from app.reports.backup_source import validate_bundle

        source_sha = validate_bundle(config.repo, config.source_manifest_sha256).sha
    else:
        raise BackupError("UNKNOWN_SOURCE_KIND")
    # Settings parsing does not import the DB engine or start the application.
    from app.config import Settings

    settings = Settings(_env_file=config.repo / ".env")
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or url.query or not url.database:
        raise BackupError("UNSUPPORTED_DATABASE")
    if regular(Path(url.database)) != regular(config.repo / "data/moderation.db"):
        raise BackupError("DATABASE_OUTSIDE_BACKUP_SCOPE")
    if settings.ai_prompt_rules_file:
        prompt = Path(settings.ai_prompt_rules_file)
        if not prompt.is_absolute():
            prompt = config.repo / prompt
        if not regular(prompt).is_relative_to(config.repo / "config"):
            raise BackupError("PROMPT_OUTSIDE_BACKUP_SCOPE")
        if config.mode == "plain" and regular(prompt) != regular(
            config.repo / "config/ai_prompt_rules.txt"
        ):
            raise BackupError("PROMPT_OUTSIDE_PUBLIC_CONFIG_SCOPE")
    if _current_revision() != config.expected_revision:
        raise BackupError("SOURCE_SCHEMA_MISMATCH")
    return source_sha


def cleanup_running() -> bool:
    if sys.platform != "win32":
        return False
    # Explicit Windows module path avoids inheriting PowerShell 7's module paths.
    env = dict(os.environ)
    windir = env.get("SystemRoot", r"C:\Windows")
    env["PSModulePath"] = str(Path(windir) / "System32/WindowsPowerShell/v1.0/Modules")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; $t=Get-ScheduledTask -ErrorAction Stop | Where-Object {$_.TaskName -eq 'QQBotAutoCleanup'}; if($t -and 'Running' -in @($t.State)){exit 3}; exit 0",
        ],
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in (0, 3):
        raise BackupError("CLEANUP_STATE_UNKNOWN")
    return result.returncode == 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    for name in ("repo", "destination", "state"):
        init.add_argument("--" + name, type=Path, required=True)
    init.add_argument("--owner-sid")
    init.add_argument("--mode", choices=("plain", "encrypted"), default="plain")
    init.add_argument("--source-kind", choices=("git", "bundle"), default="git")
    init.add_argument("--manifest-sha256", default="")
    for name in ("run", "status", "restore"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "restore":
            command.add_argument("--snapshot", required=True)
            command.add_argument("--to", type=Path, required=True)
    args = parser.parse_args(argv)
    config: BackupConfig | None = None
    result: dict[str, Any]
    try:
        if args.command == "init":
            initialize(
                args.repo,
                args.destination,
                args.state,
                args.owner_sid,
                mode=args.mode,
                source_kind=args.source_kind,
                manifest_sha256=args.manifest_sha256,
            )
            result = {"status": "initialized"}
        else:
            config = read_config(args.config)
            if args.command == "run":
                source_sha = validate_source(config)
                if cleanup_running():
                    raise BackupError("CLEANUP_RUNNING")
                result = run_backup(config, source_sha=source_sha)
            elif args.command == "restore":
                result = restore_snapshot(config, args.snapshot, args.to)
            else:
                result = {"status": "status", "last_success": None, "last_attempt": None}
                for field, root in (
                    ("last_success", config.destination),
                    ("last_attempt", config.state_dir),
                ):
                    path = checked(root / (field + ".json"), exists=False)
                    if path.exists():
                        result[field] = json.loads(regular(path).read_text(encoding="utf-8"))
                return_code = 0 if result["last_success"] else 2
                print(json.dumps(result, ensure_ascii=False))
                return return_code
        if config and args.command == "run":
            _json(config.state_dir / "last_attempt.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Raw exceptions may contain URLs, tokens, contents, or private filenames.
        result = {
            "status": "failed",
            "error": str(exc) if isinstance(exc, BackupError) else "BACKUP_COMMAND_FAILED",
            "at": datetime.now(UTC).isoformat(),
        }
        if config and args.command == "run":
            with suppress(OSError):
                _json(config.state_dir / "last_attempt.json", result)
        print(json.dumps(result))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
