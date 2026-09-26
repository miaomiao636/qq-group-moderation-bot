"""Explicitly authorize NEW OneBot groups (>=200), with owner approval and live identity.

Only absent settings/owner/route triples can be created; existing rows are never
overwritten. Dry-run is the default. This is not a future automatic enrollment rule.
Backup and immutable prepared audit precede commit. No messages are sent here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.reports.backup import backup_sqlite  # noqa: E402
from app.reports.backup_paths import private_directory  # noqa: E402
from app.space_inspector.directory import from_local_config  # noqa: E402
from scripts.enable_group_actions import (  # noqa: E402
    configured_self_id,
    declared_stage,
    route_ready,
)


def select_targets(groups: list[dict], requested: list[str]) -> list[dict]:
    if not requested or len(requested) > 50 or len(set(requested)) != len(requested):
        raise ValueError("Require 1-50 distinct explicit group IDs")
    by_id = {g["group_id"]: g for g in groups}
    if len(by_id) != len(groups):
        raise ValueError("Ambiguous group snapshot")
    targets = []
    for gid in requested:
        if not re.fullmatch(r"[1-9][0-9]{4,11}", gid) or gid not in by_id:
            raise ValueError("Target missing from live directory")
        group = by_id[gid]
        if type(group["member_count"]) is not int or group["member_count"] < 200:
            raise ValueError("Target must currently have at least 200 members")
        if not isinstance(group["name"], str) or len(group["name"]) > 64:
            raise ValueError("Group name does not fit settings schema")
        targets.append(dict(group))
    return targets


def assert_absent(con: sqlite3.Connection, targets: list[dict]) -> None:
    for g in targets:
        for table in ("provider_group_settings", "group_action_owners", "group_provider_routes"):
            # Any provider's row blocks this narrowly scoped initializer.
            if con.execute(
                f"SELECT 1 FROM {table} WHERE external_group_id=?", (g["group_id"],)
            ).fetchone():
                raise ValueError(f"Existing authorization state: {g['group_id']}/{table}")


def write_receipt(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())


def verify_committed(con: sqlite3.Connection, receipt: dict) -> None:
    """Verify exact authorized state, not just the continued presence of a route."""
    con.execute("BEGIN")
    try:
        if (
            con.execute(
                "SELECT count(*) FROM admin_audits WHERE action='initialize_group_actions' AND target_id=?",
                (receipt["op_id"],),
            ).fetchone()[0]
            != 1
        ):
            raise RuntimeError("Commit receipt unavailable; inspect state before retry")
        for g in receipt["targets"]:
            gid = g["group_id"]
            settings = con.execute(
                "SELECT provider,name,moderation_enabled,action_enabled,version,updated_at FROM provider_group_settings WHERE external_group_id=?",
                (gid,),
            ).fetchall()
            owners = con.execute(
                "SELECT provider FROM group_action_owners WHERE external_group_id=?", (gid,)
            ).fetchall()
            routes = con.execute(
                "SELECT message_provider,action_provider,updated_at FROM group_provider_routes WHERE external_group_id=?",
                (gid,),
            ).fetchall()
            stamp = receipt["row_updated_at"]
            if (
                settings != [("onebot", g["name"], 1, 1, 1, stamp)]
                or owners != [("onebot",)]
                or routes != [("onebot", "onebot", stamp)]
            ):
                raise RuntimeError("Committed authorization changed; inspect state before retry")
    finally:
        con.rollback()


def initialize(
    con: sqlite3.Connection,
    targets: list[dict],
    *,
    self_id: str,
    authorization: str,
    audit_path: Path,
    verify_identity: Callable[[], tuple[str, str]],
    context: dict | None = None,
) -> dict:
    targets = select_targets(targets, [g["group_id"] for g in targets])
    if not authorization.strip() or not re.fullmatch(r"[1-9][0-9]{4,11}", self_id):
        raise ValueError("Explicit owner authorization and account binding required")
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    op_id = uuid.uuid4().hex
    receipt = {
        "op_id": op_id,
        "created_at": stamp,
        "provider": "onebot",
        "self_id": self_id,
        "authorization": authorization,
        "operator": "agent:explicit-owner-authorization",
        "stage_declared": "recall_only",
        "stage_runtime_proven": False,
        "before": "all target settings/owners/routes absent",
        "targets": targets,
        "status": "PREPARED_NOT_COMMIT_PROOF",
        "context": context or {},
        "rollback_policy": "Only remove this operation's exact new rows after checking version=1 and matching updated_at/owner/route; never restore the old whole database over new messages.",
        "row_updated_at": stamp,
    }
    con.execute("BEGIN IMMEDIATE")
    try:
        if verify_identity() != (self_id, "recall_only"):
            raise ValueError("Live account or declared stage changed")
        assert_absent(con, targets)
        for g in targets:
            gid = g["group_id"]
            con.execute(
                "INSERT INTO group_action_owners(external_group_id,provider) VALUES (?,'onebot')",
                (gid,),
            )
            con.execute(
                "INSERT INTO group_provider_routes(external_group_id,message_provider,action_provider,updated_at) VALUES (?,'onebot','onebot',?)",
                (gid, stamp),
            )
            con.execute(
                "INSERT INTO provider_group_settings(provider,external_group_id,name,moderation_enabled,action_enabled,version,updated_at) VALUES ('onebot',?,?,1,1,1,?)",
                (gid, g["name"], stamp),
            )
            if not route_ready(con, "onebot", gid):
                raise RuntimeError("Inserted route not ready")
        con.execute(
            "INSERT INTO admin_audits(operator,action,target_type,target_id,detail_json,created_at) VALUES (?,?,?,?,?,?)",
            (
                receipt["operator"],
                "initialize_group_actions",
                "onebot_groups",
                op_id,
                json.dumps(receipt, ensure_ascii=False),
                stamp,
            ),
        )
        write_receipt(audit_path, receipt)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    # The database audit row, observed on a fresh connection, is commit proof.
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", action="append", required=True)
    parser.add_argument("--db", type=Path, default=ROOT / "data/moderation.db")
    parser.add_argument("--config-dir", type=Path, default=Path("D:/QQ/config"))
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--authorized-by", default="")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    self_id = configured_self_id()
    if not self_id or declared_stage() != "recall_only":
        parser.error("Configured account and recall_only stage required")
    if args.execute and (not args.authorized_by.strip() or args.evidence_dir is None):
        parser.error("Execution requires --authorized-by and --evidence-dir on D/E")
    if args.execute and args.evidence_dir.resolve().drive.upper() not in {"D:", "E:"}:
        parser.error("Execution evidence and database backup must use D/E")
    directory = from_local_config(args.config_dir, self_id)
    try:

        def observe() -> list[dict]:
            return select_targets(
                [
                    {"group_id": g.group_id, "name": g.name, "member_count": g.member_count}
                    for g in directory.groups()
                ],
                args.group,
            )

        targets = observe()
        db = args.db.resolve(strict=True)
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as read:
            assert_absent(read, targets)
        print(
            json.dumps(
                {"self_id": self_id, "stage_declared": "recall_only", "targets": targets},
                ensure_ascii=False,
            )
        )
        if not args.execute:
            print("DRY_RUN_OK; no database changes")
            return 0
        out = private_directory(args.evidence_dir.resolve() / uuid.uuid4().hex)
        backup = backup_sqlite(f"sqlite:///{db}", destination_dir=out)
        fresh = observe()
        if fresh != targets or configured_self_id() != self_id:
            raise ValueError("Directory/account changed after backup; re-preview required")

        def verify() -> tuple[str, str]:
            directory.verify_identity()
            return configured_self_id(), declared_stage()

        with sqlite3.connect(db, timeout=10) as con:
            receipt = initialize(
                con,
                fresh,
                self_id=self_id,
                authorization=args.authorized_by,
                audit_path=out / "prepared.json",
                verify_identity=verify,
                context={
                    "backup": str(backup),
                    "snapshot_sha256": hashlib.sha256(
                        json.dumps(fresh, sort_keys=True).encode()
                    ).hexdigest(),
                    "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                },
            )
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as check:
            verify_committed(check, receipt)
        write_receipt(out / "committed.json", {**receipt, "status": "COMMITTED_VERIFIED"})
        print(f"COMMITTED_VERIFIED {out}")
        return 0
    finally:
        directory.close()


if __name__ == "__main__":
    raise SystemExit(main())
