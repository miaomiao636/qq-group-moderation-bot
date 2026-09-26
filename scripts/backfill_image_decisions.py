"""Explicit A2 backfill. Dry-run is the default; never migrates the schema."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.image_decision_authority import AuthorityError, backfill  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    try:
        print(json.dumps(backfill(args.db, apply=args.apply), ensure_ascii=False, indent=2))
        return 0
    except (AuthorityError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"BACKFILL_FAILED {exc}")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
