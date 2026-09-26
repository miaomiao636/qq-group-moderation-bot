"""Registered A2 synthetic schema/provenance adapters, never used by production."""

import json
import sqlite3

from scripts import image_decision_authority as authority


def initialize_authority(db):
    with sqlite3.connect(db) as con:
        existing = {r[1] for r in con.execute("PRAGMA table_info(image_allowlist)")}
        columns = {
            "decision_state": "TEXT NOT NULL DEFAULT ''",
            "decision_source": "TEXT NOT NULL DEFAULT ''",
            "decision_operator": "TEXT NOT NULL DEFAULT ''",
            "decision_version": "INTEGER NOT NULL DEFAULT 0",
            "decided_at": "TEXT",
            "history_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in columns.items():
            if name not in existing:
                con.execute(f"ALTER TABLE image_allowlist ADD COLUMN {name} {definition}")
        con.execute(
            "CREATE TABLE IF NOT EXISTS system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
    authority.backfill(db, apply=True)


def decide(db, value, state, source="review", note=""):
    authority.apply_decisions(
        db,
        [authority.Decision(value, state, source, "synthetic", note)],
        expected_versions=authority.versions(db, {value}),
    )


def identity_snapshot(verifier, db, phash, entry):
    """Valid source fixtures change authority; malformed exports only change the export."""
    if isinstance(entry, dict) and entry.get("state", "rejected") in {"approved", "rejected"}:
        decide(
            db,
            phash,
            "allowed" if entry.get("state") == "approved" else "rejected",
            entry.get("source", ""),
        )
    else:
        verifier.rejection_snapshot_path(db).write_text(
            json.dumps({phash: entry}), encoding="utf-8"
        )
