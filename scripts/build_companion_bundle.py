"""Build a reviewable companion source bundle from a fixed Git commit, not live files.

This command never publishes a release, copies local configuration, or deploys services.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

REQUIRED_FILES = frozenset(
    {
        "DELIVERY.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        ".env.example",
        "alembic.ini",
        "config/ai_prompt_rules.txt",
        "docs/delivery/group-management.md",
        "docs/delivery/space-inspector.md",
        "docs/delivery/acceptance-maintenance.md",
        "scripts/install-services-nssm.ps1",
        "scripts/install-space-inspector.ps1",
        "scripts/napcat-autostart-template.bat",
        "scripts/register_backup_task.ps1",
        "scripts/image_decision_authority.py",
        "scripts/image_allowlist_seed.py",
        "scripts/retention_audit.py",
    }
)
RUNTIME_SUFFIXES = frozenset({".py", ".mako"})


class BundleError(Exception):
    """A source or destination cannot be used safely for this candidate."""


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if result.returncode:
        raise BundleError("Git source lookup failed; check the repository and commit.")
    return result.stdout


def _sources(repo: Path, ref: str) -> tuple[str, dict[str, bytes]]:
    sha = (
        _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    )
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise BundleError("Expected a full Git commit SHA.")
    tree = _git(repo, "ls-tree", "-rz", "--full-tree", sha)
    selected: dict[str, str] = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, name_bytes = entry.split(b"\t", 1)
        name = name_bytes.decode("utf-8")
        runtime = name.startswith(("app/", "alembic/"))
        if name not in REQUIRED_FILES and not runtime:
            continue
        path = PurePosixPath(name)
        mode, kind, oid = metadata.decode().split()
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or path.is_absolute()
            or ".." in path.parts
            or any(char in name for char in '\\:<>"|?*')
            or (
                runtime
                and (
                    path.suffix not in RUNTIME_SUFFIXES
                    or any(p.startswith(".") for p in path.parts)
                )
            )
        ):
            raise BundleError(f"Unreviewed file or link in delivery inputs: {name}")
        selected[name] = oid
    missing = REQUIRED_FILES.difference(selected)
    if missing:
        raise BundleError("Missing required delivery files: " + ", ".join(sorted(missing)))
    if not any(name.startswith("app/") for name in selected) or not any(
        name.startswith("alembic/") for name in selected
    ):
        raise BundleError("Missing application or migration source tree.")
    # Raw blobs avoid worktree data and archive export-subst/export-ignore filters.
    names = sorted(selected)
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input="".join(selected[name] + "\n" for name in names).encode("ascii"),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise BundleError("Could not read committed delivery files.")
    stream = io.BytesIO(result.stdout)
    files = {}
    for name in names:
        header = stream.readline().decode("ascii").strip().split()
        if len(header) != 3 or header[:2] != [selected[name], "blob"] or not header[2].isdigit():
            raise BundleError("Invalid committed file response.")
        size = int(header[2])
        content = stream.read(size)
        if len(content) != size or stream.read(1) != b"\n":
            raise BundleError("Incomplete committed file response.")
        files[name] = content
    return sha, files


def build(repo: Path, ref: str, output: Path) -> dict[str, object]:
    if output.exists():
        raise BundleError("Output already exists; choose a new filename.")
    sha, files = _sources(repo, ref)
    for line in files[".env.example"].decode("utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        if separator and re.search(r"(?:^|_)(?:PASSWORD|TOKEN|SECRET|API_KEY)$", key.strip()):
            if value.strip().strip("\"'"):
                raise BundleError("A credential field in the public template is not empty.")
    files["README.md"] = (
        "# QQ 群管理与空间巡检配套交付候选包\n\n"
        f"来源提交：`{sha}`。本包尚未完成接收方现场验收。\n\n"
        "请先阅读 [配套交付说明](DELIVERY.md)，再按对应手册安装。\n\n"
        "包内不含 QQ/NapCat、Python、uv、Edge 安装程序或任何登录会话。\n"
        "本包不含 Git 元数据，现有依赖 Git 的每日备份流程不能直接用于此目录。\n"
    ).encode()
    manifest = {
        "format": 1,
        "status": "candidate_not_accepted",
        "source_sha": sha,
        "components": ["group_management", "space_inspector"],
        "release_blockers": [
            "recipient_clean_install_and_recovery_not_verified",
            "napcat_only_service_installer_requires_adaptation",
            "gitless_scheduled_backup_not_supported",
            "inspector_data_not_in_main_backup_scope",
            "license_metadata_requires_owner_confirmation",
        ],
        "files": [
            {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for name, content in sorted(files.items())
        ],
    }
    files["DELIVERY-MANIFEST.json"] = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    buffer = io.BytesIO()
    prefix = f"qqbot-companion-{sha[:12]}/"
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(prefix + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    payload = buffer.getvalue()
    try:
        with output.open("xb") as stream:
            stream.write(payload)
    except FileExistsError:
        raise BundleError("Output already exists; choose a new filename.") from None
    return {
        "source_sha": sha,
        "status": manifest["status"],
        "output": str(output.resolve()),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "file_count": len(files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = build(args.repo, args.ref, args.output)
    except (BundleError, OSError, UnicodeError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Bundle not completed: {exc}\n")
    print(json.dumps(receipt, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
