"""Synthetic migration, rollback and old-schema maintenance compatibility."""

import os
import sqlite3
import subprocess
import sys

import pytest
from app.config import PROJECT_ROOT
from app.reports.backup import backup_sqlite

OLD = "e1c7d4b8a902"
NEW = "f3c8a9d12064"


def test_additive_migration_preserves_history_backup_and_old_schema_jobs(tmp_path):
    database = tmp_path / "synthetic.db"
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "PYTHONUTF8": "1",
    }

    def run(*args):
        result = subprocess.run(
            [sys.executable, *args],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def migrate(direction, target):
        run("-m", "alembic", "-c", str(PROJECT_ROOT / "alembic.ini"), direction, target)

    migrate("upgrade", OLD)
    with sqlite3.connect(database) as con:
        con.execute(
            "INSERT INTO system_settings(key,value,updated_at) VALUES('auto_cleanup_enabled','1',CURRENT_TIMESTAMP)"
        )
        con.execute("""INSERT INTO action_intents
            (id,idempotency_key,action,status,group_openid,target_member_openid,message_id,
             provider,external_group_id,external_user_id,external_message_id,
             params_json,result_json,reason,actor,created_at,updated_at)
            VALUES(1,'test','recall','SUCCEEDED','123','456','789','onebot','123','456','789',
                   '{}','{}','','system',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""")
    # The short-lived cleanup/report jobs may use new source before deployment.
    assert '"status": "succeeded"' in run("-m", "app.reports.maintenance", "cleanup")
    assert '"onebot_recall_untracked": 1' in run(
        "-c",
        """
import asyncio,json
from datetime import UTC,datetime
from app.db import SessionLocal,engine
from app.reports.service import build_daily
async def main():
    async with SessionLocal() as s: print(json.dumps(await build_daily(s, datetime.now(UTC))))
    await engine.dispose()
asyncio.run(main())
""",
    )
    migrate("upgrade", NEW)
    with sqlite3.connect(database) as con:
        con.execute("PRAGMA foreign_keys=ON")
        assert con.execute("SELECT count(*) FROM recall_confirmations").fetchone() == (0,)
        con.execute("""INSERT INTO recall_confirmations
            (intent_id,account_id,group_id,user_id,message_id,source_event_key,source_sent_at,
             requested_at,confirmed_at,notice_at,operator_id,notice_fingerprint)
            VALUES(1,'10000001','123','456','789','789',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,
                   CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'10000001','test-fingerprint')""")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE recall_confirmations SET intent_id=999")
    copied = backup_sqlite(f"sqlite:///{database}", destination_dir=tmp_path / "restore")
    with sqlite3.connect(copied) as con:
        assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("SELECT version_num FROM alembic_version").fetchone() == (NEW,)
        assert con.execute("SELECT notice_fingerprint FROM recall_confirmations").fetchone() == (
            "test-fingerprint",
        )
    migrate("downgrade", OLD)
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT status FROM action_intents").fetchone() == ("SUCCEEDED",)
    migrate("upgrade", NEW)
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT count(*) FROM recall_confirmations").fetchone() == (0,)
        assert con.execute("SELECT status FROM action_intents").fetchone() == ("SUCCEEDED",)
    # Invalid metadata is never treated as the one compatible predecessor.
    with sqlite3.connect(database) as con:
        con.execute("UPDATE alembic_version SET version_num='unsupported'")
    result = subprocess.run(
        [sys.executable, "-m", "app.reports.maintenance", "cleanup"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode != 0 and "startup_or_metadata_failed" in result.stdout
