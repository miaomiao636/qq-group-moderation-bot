"""Deployment-driven maintenance: ``python -m app.reports.maintenance cleanup``.

Windows Task Scheduler supplies the interval; this module installs no task/thread.
The last attempt, outcome and deletion counts answer whether scheduling ran,
whether cleanup was enabled, and whether an operator must investigate failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class CleanupOutcome(TypedDict):
    status: Literal["skipped", "succeeded", "failed"]
    counts: dict[str, int]
    error: str


_COUNT_KEYS = frozenset(
    {
        "processed_events_deleted",
        "violation_snapshots_purged",
        "violation_evidence_purged",
        "case_reasons_purged",
        "shadow_content_purged",
        "feedback_content_purged",
        "candidate_patterns_purged",
        "ai_cache_deleted",
        "action_logs_deleted",
        "media_files_deleted",
        "inbox_payloads_purged",
        "inbox_records_deleted",
        "notification_notices_deleted",
        "notification_deliveries_deleted",
        "cases_archived",
        "cases_purged",
        "cases_cleanup_deferred",
        "violation_records_purged",
        "ai_usage_logs_deleted",
        "candidates_expired",
        # "15 天全副本工程"：登记式副本清理计数（2026-09-15）
        "managed_copy_files_deleted",
        "managed_copy_bytes_freed",
        "managed_copy_dirs_removed",
    }
)


async def run_cleanup(session: AsyncSession) -> CleanupOutcome:
    """Respect the persistent opt-in switch and save only safe attempt metadata.

    A missing/non-1 switch skips deletion, but still records scheduler activity.
    ``last_cleanup_at`` means last attempt, not last successful cleanup. Existing
    purge_expired owns its commit/media deletion, so failure may mean partial
    filesystem progress; callers must not describe failure as an atomic rollback.
    """
    from sqlalchemy.dialects.sqlite import insert

    from app.models import SystemSetting
    from app.reports.cleanup import purge_expired

    outcome: CleanupOutcome = {"status": "skipped", "counts": {}, "error": ""}
    try:
        enabled = await session.get(SystemSetting, "auto_cleanup_enabled", populate_existing=True)
        if enabled is not None and enabled.value == "1":
            counts = await purge_expired(session)
            # Never forward arbitrary keys or values into metadata/log output.
            if any(
                key not in _COUNT_KEYS or type(value) is not int or value < 0
                for key, value in counts.items()
            ):
                raise ValueError("invalid cleanup counters")
            outcome = {"status": "succeeded", "counts": counts, "error": ""}
    except Exception:
        await session.rollback()
        outcome = {"status": "failed", "counts": {}, "error": "cleanup_failed"}

    values = {
        "last_cleanup_at": datetime.now(UTC).isoformat(),
        "last_cleanup_status": outcome["status"],
        "last_cleanup_result": json.dumps(outcome["counts"], separators=(",", ":"), sort_keys=True),
        "last_cleanup_error": outcome["error"],
    }
    for key, value in values.items():
        statement = insert(SystemSetting).values(key=key, value=value)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[SystemSetting.key],
                set_={"value": statement.excluded.value, "updated_at": datetime.now(UTC)},
            )
        )
    await session.commit()
    return outcome


async def _run_cleanup() -> CleanupOutcome:
    # Lazy imports keep configuration/connection errors inside the safe CLI error
    # boundary: raw exception text can include configuration or database content.
    from app.db import SessionLocal, check_db_migrated, engine

    try:
        await check_db_migrated()
        async with SessionLocal() as session:
            return await run_cleanup(session)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an explicitly enabled maintenance task.")
    parser.add_argument("command", choices=["cleanup"])
    parser.parse_args(argv)
    try:
        outcome = asyncio.run(_run_cleanup())
    except Exception:
        # If the database/configuration itself is unavailable, metadata cannot be
        # persisted. Scheduler exit status and this fixed code remain observable.
        outcome = {"status": "failed", "counts": {}, "error": "startup_or_metadata_failed"}
    print(
        json.dumps(
            {
                "event": "maintenance_cleanup",
                "entry_point": "cli",
                "run_id": uuid.uuid4().hex,
                **outcome,
            },
            sort_keys=True,
        )
    )
    return 1 if outcome["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
