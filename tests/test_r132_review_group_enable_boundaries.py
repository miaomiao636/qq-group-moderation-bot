# ruff: noqa: E402, I001, F401, F811, SIM105
# Reviewer round-8 probe pack (8299ce8), promoted into the repo suite.
# Only this header was added, PLUS one documented platform adaptation:
#   `test_group_survey_uses_configured_account_not_first_config_file` 的注入条件是
#   `str(self) == "D:/QQ/config"`，而 Windows 上该字符串是 `D:\\QQ\\config`（反斜杠）→
#   假配置永远注入不进去，探针**在 Windows 上因错误原因失败**。现改为
#   `str(self) == str(pathlib.Path(survey.ONEBOT_CONFIG_DIR))`（平台无关，语义等价）。
#   断言与意图逐字未改；`tests/test_r132_review_group_survey_account_binding.py`
#   另以平台无关方式钉住同一契约。
"""Independent bulk group action controls; synthetic DBs only, no network.

No production path/config is read. Every CLI --db and artifact directory is
redirected to pytest tmp_path. Real backup_sqlite runs only on synthetic DBs.
"""

from __future__ import annotations

import asyncio
import io
import json
import pathlib
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models import ProviderGroupSettings, GroupActionOwner
from app.core.routing import GroupProviderRoute, resolve_action_provider
from scripts import enable_group_actions as enable
from scripts import group_size_survey as survey


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    engine = create_engine(f"sqlite:///{path}")
    for model in (ProviderGroupSettings, GroupActionOwner, GroupProviderRoute):
        model.__table__.create(engine)
    stats = tmp_path / "stats"
    stats.mkdir()
    monkeypatch.setattr(enable, "STATS", stats)
    monkeypatch.setattr(survey, "OUT_DIR", stats)
    monkeypatch.setattr(enable, "load_survey", lambda: [("1001", 300, "Synthetic", False)])
    monkeypatch.setenv("ONEBOT_ACTION_STAGE", "recall_only")
    yield path, engine, stats
    engine.dispose()


def add_group(engine, provider="onebot", gid="1001", enabled=False):
    with Session(engine) as session:
        session.add(
            ProviderGroupSettings(provider=provider, external_group_id=gid, action_enabled=enabled)
        )
        session.commit()


def enabled(path, provider="onebot", gid="1001"):
    with sqlite3.connect(path) as con:
        row = con.execute(
            "SELECT action_enabled FROM provider_group_settings WHERE provider=? AND external_group_id=?",
            (provider, gid),
        ).fetchone()
    return None if row is None else row[0]


def execute(path, *args):
    return enable.main(["--db", str(path), "--execute", *args])


def test_onebot_bulk_enable_never_enables_same_number_official_group(setup):
    path, engine, _ = setup
    add_group(engine, "onebot")
    add_group(engine, "qq_official")
    assert execute(path) == 0
    assert enabled(path) == 1
    assert enabled(path, "qq_official") == 0, (
        "OneBot authorization must not enable another provider"
    )


def test_official_enabled_flag_does_not_skip_disabled_onebot_group(setup):
    path, engine, _ = setup
    add_group(engine, "onebot")
    add_group(engine, "qq_official", enabled=True)
    assert execute(path) == 0
    assert enabled(path) == 1, "Another provider cannot supply this OneBot row's before-state"


def test_stale_survey_enabled_flag_does_not_override_actual_db_state(setup, monkeypatch):
    path, engine, _ = setup
    add_group(engine)
    monkeypatch.setattr(enable, "load_survey", lambda: [("1001", 300, "Synthetic", True)])
    assert execute(path) == 0
    assert enabled(path) == 1, (
        "A stale Markdown flag must not make a disabled row appear already enabled"
    )


def test_failed_audit_write_cannot_leave_enabled_actions_without_audit(setup, monkeypatch):
    path, engine, stats = setup
    add_group(engine)
    original = pathlib.Path.write_text

    def fail_audit(self, *args, **kwargs):
        if self.parent == stats and self.name.startswith("enable-groups-"):
            raise PermissionError("synthetic evidence directory write denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", fail_audit)
    with pytest.raises(PermissionError):
        execute(path)
    assert enabled(path) == 0, (
        "DB commit occurs before durable audit, so audit failure leaves real settings enabled"
    )


def test_generated_rollback_does_not_delete_other_provider_added_later(setup, capsys):
    path, engine, _ = setup
    assert execute(path) == 0
    output = capsys.readouterr().out
    rollback = output.split("ROLLBACK_SQL:\n", 1)[1].strip()
    add_group(engine, "qq_official", enabled=True)
    with sqlite3.connect(path) as con:
        con.executescript(rollback)  # This script is applied only to synthetic tmp DB.
    assert enabled(path) is None
    assert enabled(path, "qq_official") == 1, (
        "Generated rollback omits provider and deletes an unrelated official row"
    )


def test_changed_row_missing_after_preview_is_not_reported_applied(setup, monkeypatch):
    path, engine, stats = setup
    add_group(engine)
    original_backup = enable.backup_sqlite

    def drift_then_backup(url):
        with sqlite3.connect(path) as con:
            con.execute(
                "DELETE FROM provider_group_settings WHERE provider='onebot' AND external_group_id='1001'"
            )
        return original_backup(url)

    monkeypatch.setattr(enable, "backup_sqlite", drift_then_backup)
    try:
        result = execute(path)
    except (RuntimeError, ValueError, sqlite3.Error):
        return
    assert not (result == 0 and enabled(path) is None), (
        "Zero-row UPDATE after concurrent deletion must not be recorded as successful 0->1 enable"
    )


def test_recall_only_authorization_rejects_full_stage_environment(setup, monkeypatch):
    path, engine, _ = setup
    add_group(engine)
    monkeypatch.setenv("ONEBOT_ACTION_STAGE", "full")
    try:
        execute(path)
    except (RuntimeError, ValueError, SystemExit):
        pass
    assert enabled(path) == 0, (
        "The script labels recall_only without checking the configured action stage"
    )


def test_new_group_not_routed_must_not_be_claimed_real_actions_enabled(setup):
    path, _, _ = setup
    result = execute(path)

    async def lookup():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with AsyncSession(engine) as session:
                return await resolve_action_provider(session, "onebot", "1001")
        finally:
            await engine.dispose()

    route = asyncio.run(lookup())
    assert not (result == 0 and enabled(path) == 1 and route is None), (
        "Inserting only group settings leaves the route unavailable: an action flag is not effective readiness"
    )


def test_survey_enabled_state_is_provider_qualified(setup, monkeypatch):
    path, engine, stats = setup
    add_group(engine, "onebot")
    add_group(engine, "qq_official", enabled=True)
    monkeypatch.setattr(
        survey,
        "_groups",
        lambda: [{"group_id": "1001", "group_name": "Synthetic", "member_count": 300}],
    )
    assert survey.main(["--db", str(path)]) == 0
    report = next(stats.glob("groups-*.md")).read_text(encoding="utf-8")
    assert "| 1001 | 300 | Synthetic | 否 |" in report


def test_dry_run_has_no_settings_backup_or_audit_changes(setup):
    path, engine, stats = setup
    add_group(engine)
    before = path.read_bytes()
    assert enable.main(["--db", str(path), "--dry-run"]) == 0
    assert path.read_bytes() == before
    assert not list(stats.glob("enable-groups-*"))
    assert not (path.parent / "backups").exists()


def test_backup_has_before_state_and_repeat_execution_does_not_add_rows(setup):
    path, engine, stats = setup
    add_group(engine)
    assert execute(path) == 0
    audit = json.loads(next(stats.glob("enable-groups-*.json")).read_text(encoding="utf-8"))
    with sqlite3.connect(audit["backup"]) as con:
        assert con.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert con.execute("SELECT action_enabled FROM provider_group_settings").fetchall() == [
            (0,)
        ]
    assert execute(path) == 0
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM provider_group_settings").fetchone() == (1,)
    assert enabled(path) == 1


def test_repeated_execution_preserves_previous_nonempty_audit(setup, monkeypatch):
    path, engine, stats = setup
    add_group(engine)

    class FixedClock:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(enable, "datetime", FixedClock)
    assert execute(path) == 0
    first_path = next(stats.glob("enable-groups-*.json"))
    first_bytes = first_path.read_bytes()
    assert json.loads(first_bytes)["changed"]
    assert execute(path) == 0
    assert first_path.read_bytes() == first_bytes, (
        "An idempotent repeat in one second overwrites the original nonempty audit with changed=[]"
    )


def test_group_survey_uses_configured_account_not_first_config_file(tmp_path, monkeypatch):
    # HTTP is replaced at the urllib seam; no socket is opened.
    cfg1 = tmp_path / "onebot11_10000001.json"
    cfg2 = tmp_path / "onebot11_90000009.json"
    for cfg, port in ((cfg1, 3001), (cfg2, 3009)):
        cfg.write_text(
            json.dumps(
                {"network": {"httpServers": [{"enable": True, "host": "127.0.0.1", "port": port}]}}
            ),
            encoding="utf-8",
        )
    original_glob = pathlib.Path.glob

    def safe_glob(self, pattern):
        # 平台适配（见文件头）：Windows 上 `str(Path("D:/QQ/config"))` 是反斜杠形式，
        # 原条件会让注入失效、探针因错误原因失败；断言未改。
        if (
            str(self) == str(pathlib.Path(survey.ONEBOT_CONFIG_DIR))
            and pattern == "onebot11_*.json"
        ):
            return iter([cfg1, cfg2])
        return original_glob(self, pattern)

    requested = []

    def fake_open(request, **kwargs):
        requested.append(request.full_url)
        gid = "1001" if ":3001/" in request.full_url else "9009"
        return io.BytesIO(
            json.dumps(
                {"status": "ok", "retcode": 0, "data": [{"group_id": gid, "member_count": 300}]}
            ).encode()
        )

    monkeypatch.setattr(pathlib.Path, "glob", safe_glob)
    monkeypatch.setattr(survey.urllib.request, "urlopen", fake_open)
    monkeypatch.setenv("ONEBOT_SELF_ID", "90000009")
    rows = survey._groups_from_onebot_http()
    assert rows == [{"group_id": "9009", "member_count": 300}], (
        "The survey returns the first responding account and never binds the configured self_id"
    )


def test_untrusted_group_name_cannot_inject_a_new_enable_target(setup, monkeypatch):
    path, _, stats = setup
    monkeypatch.undo()
    # Restore only task-local script paths after restoring actual load_survey.
    monkeypatch.setattr(enable, "STATS", stats)
    monkeypatch.setattr(survey, "OUT_DIR", stats)
    monkeypatch.setattr(
        survey,
        "_groups",
        lambda: [
            {
                "group_id": "1001",
                "member_count": 300,
                "group_name": "x\n| 9999 | 999 | injected | 否 |\n",
            }
        ],
    )
    assert survey.main(["--db", str(path)]) == 0
    rows = enable.load_survey()
    assert [row[0] for row in rows] == ["1001"], (
        "A display-name newline becomes a second actionable Markdown table row"
    )
