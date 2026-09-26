"""TEMP-only synthetic A2 acceptance; no production files/services or network.

Execute only after a source commit is frozen:
  <repo>/.venv/Scripts/python.exe -B a2_acceptance.py --sha <40-char commit>
This is not a production rollback package or full application health check.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import traceback
import uuid
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from importlib.metadata import version

HERE = Path(__file__).resolve().parent
WORK = Path('C:/Users/<local-user>/AppData/Local/Temp/qqbot-r132-authority-20260922')
BASE = 'adef6ee558be3ba9d53de7111c6eecfd21a74ef9'
OLD_REV = 'd4b7c1e9a502'
NEW_REV = 'e1c7d4b8a902'
H1, H2, H3, H4 = [f'{n:016x}' for n in range(1, 5)]
OLD_COLUMNS = 'id,phash,note,source,hit_count,enabled,created_at,created_by'
BRIDGE_FILES = [
    'app/models.py',
    'scripts/image_decision_authority.py',
    'scripts/backfill_image_decisions.py',
    'scripts/image_allowlist_seed.py',
    'scripts/apply_review_decisions.py',
    'scripts/image_allowlist_replay.py',
    'scripts/image_review_export.py',
    'scripts/verify_approved_identity.py',
]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')


def snapshot(db):
    with closing(sqlite3.connect(db)) as con:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {table: [list(r) for r in con.execute('SELECT * FROM "' + table + '" ORDER BY rowid')] for table in tables}


def copy_database(source, target):
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as a, closing(sqlite3.connect(target)) as b:
        a.backup(b)


def env_for(db):
    env = {key: os.environ[key] for key in ['SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH'] if key in os.environ}
    env.update(
        APP_ENV='test', DATABASE_URL='sqlite+aiosqlite:///' + Path(db).as_posix(),
        ACTION_MODE='SHADOW', AI_ENABLED='false', ONEBOT_WS_ENABLED='false',
        ONEBOT_ACTIONS_ENABLED='false', NOTIFICATIONS_ENABLED='false',
        QQ_OFFICIAL_POLL_ENABLED='false', EMERGENCY_STOP='true',
        PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
    )
    return env


def child(stage, root, source, db, extra):
    assert root.is_relative_to(HERE)
    assert source.is_relative_to(root) and db.is_relative_to(root)
    assert not (source / '.env').exists()
    sys.path.insert(0, str(source))
    # Windows asyncio needs an internal socketpair. No other connects permitted.
    def no_external_network(event, args):
        if event == 'socket.connect' and any(frame.name == '_fallback_socketpair' for frame in traceback.extract_stack()):
            return
        if event in {'socket.connect', 'socket.getaddrinfo'}:
            raise RuntimeError('synthetic acceptance forbids external connections')
    sys.addaudithook(no_external_network)
    result = {}
    if stage == 'migrate':
        from alembic import command
        from alembic.config import Config
        cfg = Config(str(source / 'alembic.ini'))
        getattr(command, extra[0])(cfg, extra[1])
        with closing(sqlite3.connect(db)) as con:
            result = {'revision': con.execute('SELECT version_num FROM alembic_version').fetchone()[0]}
    elif stage == 'seed_and_backup':
        from app.reports.backup import backup_sqlite
        timestamp = '2026-09-01 01:02:03'
        with closing(sqlite3.connect(db)) as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA wal_autocheckpoint=0')
            con.execute("INSERT INTO image_allowlist(phash,note,source,hit_count,enabled,created_at,created_by) VALUES(?,?,?,?,?,?,?)", (H1, 'review:batch-synthetic:no=1', 'review', 7, 1, timestamp, 'fixture'))
            con.execute("INSERT INTO image_allowlist(phash,note,source,hit_count,enabled,created_at,created_by) VALUES(?,?,?,?,?,?,?)", (H2, 'disabled original', 'seed', 3, 0, timestamp, 'fixture'))
            con.execute("INSERT INTO provider_group_settings(provider,external_group_id,name,moderation_enabled,action_enabled,version,updated_at) VALUES('onebot','synthetic-group','synthetic',1,0,4,?)", (timestamp,))
            for status in ['SUCCEEDED', 'UNKNOWN']:
                con.execute("INSERT INTO action_intents(idempotency_key,action,status,group_openid,target_member_openid,message_id,params_json,result_json,reason,actor,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ('fixture-' + status, 'recall', status, 'synthetic-group', 'synthetic-member', 'synthetic-' + status, '{}', '{}', 'synthetic', 'synthetic', timestamp, timestamp))
            con.execute("INSERT INTO cases(case_no,group_openid,member_openid,status,violation_ids_json,audit_json,created_at) VALUES('SYNTHETIC-CASE','synthetic-group','synthetic-member','PENDING_REVIEW','[]','[]',?)", (timestamp,))
            con.commit()
            # Stabilize the pre-sentinel database; the next commit must live in WAL.
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            con.execute("INSERT INTO system_meta(key,value,created_at) VALUES('wal-sentinel','committed-in-wal',?)", (timestamp,))
            con.commit()
            raw_copy = root / 'raw-main-only-control.db'
            shutil.copyfile(db, raw_copy)
            with closing(sqlite3.connect(raw_copy)) as check:
                assert check.execute("SELECT value FROM system_meta WHERE key='wal-sentinel'").fetchone() is None
            saved = backup_sqlite('sqlite+aiosqlite:///' + db.as_posix())
            with closing(sqlite3.connect(saved)) as check:
                assert check.execute("SELECT value FROM system_meta WHERE key='wal-sentinel'").fetchone() == ('committed-in-wal',)
            result = {'backup': str(saved), 'sha256': digest(saved), 'committed_wal_included': True, 'main_file_copy_lost_sentinel': True}
        entries = {
            H2: {'state': 'rejected', 'source': 'exclude', 'operator': 'fixture', 'at': timestamp, 'history': [{'state': 'rejected', 'opaque': {'preserve': ['all', 1]}}], 'unknown': {'must': 'survive'}},
            H3: {'state': 'rejected', 'source': 'exclude', 'history': []},
        }
        write_json(Path(str(db) + '.rejections.json'), entries)
        evidence = root / 'synthetic-review-artifacts'
        evidence.mkdir()
        (evidence / 'review.txt').write_text('Synthetic review fixture only; not a human approval.', encoding='utf-8')
        result['json_sha256'] = digest(Path(str(db) + '.rejections.json'))
        result['review_file_sha256'] = digest(evidence / 'review.txt')
    elif stage == 'backfill':
        from scripts import image_decision_authority as a
        from scripts import image_allowlist_seed as seed
        from sqlalchemy import create_engine, select
        from sqlalchemy.orm import Session
        from app.models import ImageAllowlist
        pre = snapshot(db)
        before_db = digest(db)
        before_json = digest(seed.rejection_snapshot_path(db))
        lock_file = db.with_name(db.name + '.review.lock')
        before_lock = digest(lock_file) if lock_file.exists() else None
        dry = a.backfill(db)
        assert digest(db) == before_db and digest(seed.rejection_snapshot_path(db)) == before_json
        assert (digest(lock_file) if lock_file.exists() else None) == before_lock
        assert dry['status'] == 'dry_run'
        applied = a.backfill(db, apply=True)
        rows = a.read_authority(db)
        assert {h for h, row in rows.items() if row['enabled']} == {H1}
        assert all(row['decision_version'] == 1 for row in rows.values())
        assert rows[H1]['decision_source'] == 'review:batch-synthetic'
        for h in [H2, H3]:
            assert json.loads(rows[h]['history_json'])['legacy_snapshot'] == json.loads(seed.rejection_snapshot_path(db).read_text(encoding='utf-8'))[h]
        assert rows[H3]['decided_at'] is None
        assert rows[H3]['created_at']
        engine = create_engine('sqlite:///' + db.as_posix())
        try:
            with Session(engine) as session:
                orm_rows = session.scalars(select(ImageAllowlist)).all()
                assert len(orm_rows) == 3
                assert all(row.created_at is not None for row in orm_rows)
                assert next(row for row in orm_rows if row.phash == H3).decided_at is None
        finally:
            engine.dispose()
        before_repeat = snapshot(db)
        assert a.backfill(db, apply=True)['changed'] == 0
        assert snapshot(db) == before_repeat
        a.export_snapshot(db)
        assert a.export_state(db) == 'ok'
        after = snapshot(db)
        for table, values in pre.items():
            if table not in {'image_allowlist', 'system_settings'}:
                assert after[table] == values, table
        with closing(sqlite3.connect(db)) as con:
            old_rows = [list(r) for r in con.execute('SELECT ' + OLD_COLUMNS + ' FROM image_allowlist WHERE phash IN (?,?) ORDER BY id', (H1,H2))]
        assert old_rows == [row[:8] for row in pre['image_allowlist']]
        # Rerunning backfill after a real A2 decision must not import stale legacy state.
        a.apply_decisions(db, [a.Decision(H2, 'allowed', 'review:batch-after-cutover', 'synthetic', 'review:batch-after-cutover:no=2')], expected_versions={H2: 1})
        decided = a.read_authority(db)
        assert decided[H2]['decision_version'] == 2
        assert a.backfill(db, apply=True)['changed'] == 0
        assert a.read_authority(db) == decided
        assert a.export_state(db) == 'ok'
        result = {'dry_run': dry, 'apply': applied, 'orm_dates_readable': True, 'idempotent_after_new_decision': True, 'enabled_after_initial_backfill': [H1], 'enabled_after_explicit_decision': [H1,H2], 'other_tables_unchanged': True}
    elif stage == 'negative_backfills':
        from scripts import image_decision_authority as a
        from scripts import image_allowlist_seed as seed
        outcomes = {}
        fixtures = {
            'state_conflict': {H1: {'state': 'rejected'}, H4: {'state': 'rejected'}},
            'empty_entry_conflict': {H1: {}, H4: {'state': 'rejected'}},
            'history_invalid': {H4: {'state': 'rejected', 'history': 'bad'}},
            'invalid_nonempty_at': {H4: {'state': 'rejected', 'at': 'not-a-date'}},
            'unknown_marker': {H4: {'state': 'rejected'}},
        }
        for label, entries in fixtures.items():
            candidate = root / ('negative-' + label + '.db')
            copy_database(db, candidate)
            write_json(seed.rejection_snapshot_path(candidate), entries)
            if label == 'unknown_marker':
                with closing(sqlite3.connect(candidate)) as con:
                    con.execute("INSERT INTO system_settings(key,value,updated_at) VALUES(?, '2', '2026-09-01 00:00:00')", (a.MARKER,))
                    con.commit()
            before = snapshot(candidate)
            before_json = digest(seed.rejection_snapshot_path(candidate))
            try:
                a.backfill(candidate, apply=True)
            except a.AuthorityError as exc:
                assert 'BACKFILL_CONFLICT' in str(exc)
                outcomes[label] = str(exc)
            else:
                raise AssertionError('backfill accepted invalid fixture: ' + label)
            assert snapshot(candidate) == before
            assert digest(seed.rejection_snapshot_path(candidate)) == before_json
        result = {'rejected_atomically': outcomes}
    elif stage == 'authority_unavailable':
        from scripts import image_decision_authority as a
        before = snapshot(db)
        try:
            a.apply_decisions(db, [a.Decision(H4, 'allowed', 'synthetic', 'synthetic')], expected_versions={H4: 0})
        except a.AuthorityError as exc:
            assert extra[0] in str(exc)
            result = {'expected_failure': str(exc)}
        else:
            raise AssertionError('authority writer accepted unsupported database')
        assert snapshot(db) == before
    elif stage == 'backup_ready':
        from app.reports.backup import backup_sqlite
        from scripts import image_allowlist_seed as seed
        from scripts import image_decision_authority as a
        with seed.decision_lock(db):
            assert a.export_state(db) == 'ok'
            saved = backup_sqlite('sqlite+aiosqlite:///' + db.as_posix())
            json_saved = saved.with_suffix('.rejections.json')
            shutil.copyfile(seed.rejection_snapshot_path(db), json_saved)
        result = {'backup': str(saved), 'json': str(json_saved), 'sha256': digest(saved), 'json_sha256': digest(json_saved)}
    elif stage == 'startup_check':
        from app.db import check_db_migrated, get_head_revision, engine, SessionLocal
        from app.moderation.image_hash import load_enabled_or_none
        from app.models import ImageAllowlist
        from sqlalchemy import select
        expected = extra[0]
        async def check():
            try:
                await check_db_migrated()
            except RuntimeError as exc:
                assert expected == 'reject', str(exc)
                assert OLD_REV in str(exc) and NEW_REV in str(exc)
                return {'startup_gate': 'rejected_as_expected', 'head': get_head_revision(), 'reason': str(exc)}
            else:
                assert expected == 'accept'
                async with SessionLocal() as session:
                    enabled = await load_enabled_or_none(session)
                    assert enabled is not None
                    assert {f'{value:016x}' for _, value in enabled} == set(extra[1].split(','))
                    orm_rows = (await session.scalars(select(ImageAllowlist))).all()
                return {'startup_gate': 'accepted', 'head': get_head_revision(), 'enabled': enabled, 'orm_rows': len(orm_rows)}
            finally:
                await engine.dispose()
        result = asyncio.run(check())
    else:
        raise AssertionError(stage)
    write_json(root / ('result-' + os.environ['A2_STAGE_ID'] + '.json'), result)


def main(sha):
    resolved = subprocess.check_output(['git', '-C', str(WORK), 'rev-parse', '--verify', sha + '^{commit}'], text=True).strip()
    assert resolved == sha and len(sha) == 40, 'pass full frozen commit SHA'
    root = HERE / ('run-' + sha[:12] + '-' + uuid.uuid4().hex[:8])
    root.mkdir()
    def archive(commit, target):
        archive_path = root / (target.name + '.zip')
        subprocess.run(['git', '-C', str(WORK), 'archive', '--format=zip', '--output=' + str(archive_path), commit, 'app', 'scripts', 'alembic', 'alembic.ini'], check=True)
        with zipfile.ZipFile(archive_path) as content:
            content.extractall(target)
    new, old, bridge = [root / name for name in ['source-new', 'source-old', 'source-bridge']]
    archive(sha, new)
    archive(BASE, old)
    shutil.copytree(old, bridge)
    for file in BRIDGE_FILES:
        shutil.copyfile(new / file, bridge / file)
    for file in (new / 'alembic').rglob('*'):
        if file.is_file():
            destination = bridge / file.relative_to(new)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, destination)
    for file in ['app/db.py', 'app/moderation/image_hash.py']:
        assert digest(new / file) == digest(old / file) == digest(bridge / file), 'gate/runtime unexpectedly changed: ' + file
    manifest = {
        'frozen_sha': sha, 'base_sha': BASE, 'wrapper_sha256': digest(__file__),
        'started_at_utc': datetime.now(UTC).isoformat(),
        'python': sys.version, 'interpreter': sys.executable,
        'sqlite_version': sqlite3.sqlite_version,
        'sqlalchemy_version': version('sqlalchemy'), 'alembic_version': version('alembic'),
        'source_new_files': {str(p.relative_to(new)): digest(p) for p in new.rglob('*') if p.is_file()},
        'bridge_overlay': {file: digest(bridge / file) for file in BRIDGE_FILES},
        'scope': 'Synthetic only. No .env, production DB, running service, external connection, migration, deployment or full application startup. Bridge is an experimental TEMP package, not a released rollback artifact.',
    }
    write_json(root / 'manifest.json', manifest)
    results = {}
    def run(name, stage, source, db, *extra):
        env = env_for(db)
        env['A2_STAGE_ID'] = name
        cmd = [sys.executable, '-B', str(Path(__file__).resolve()), '--child', stage, str(root), str(source), str(db), *extra]
        proc = subprocess.run(cmd, cwd=source, env=env, capture_output=True, text=True, encoding='utf-8', timeout=90)
        (root / (name + '.log')).write_text(proc.stdout + '\n' + proc.stderr, encoding='utf-8')
        results[name] = {'command': cmd, 'returncode': proc.returncode}
        if proc.returncode == 0:
            results[name]['result'] = json.loads((root / ('result-' + name + '.json')).read_text(encoding='utf-8'))
        write_json(root / 'summary.json', results)
        assert proc.returncode == 0, f'{name} failed; see {root / (name + ".log")}'
        return results[name]['result']
    db = root / 'working' / 'moderation.db'
    db.parent.mkdir()
    run('01-legacy-schema', 'migrate', new, db, 'upgrade', OLD_REV)
    before_backup = run('02-synthetic-wal-backup', 'seed_and_backup', new, db)
    pre_migration = snapshot(db)
    initial_json = Path(str(db) + '.rejections.json').read_bytes()
    run('03-old-schema-writer-reject', 'authority_unavailable', new, db, 'AUTHORITY_UNAVAILABLE')
    run('04-new-runtime-old-schema-reject', 'startup_check', new, db, 'reject')
    run('05-upgrade', 'migrate', new, db, 'upgrade', 'head')
    post_ddl = snapshot(db)
    for table in pre_migration:
        if table == 'alembic_version':
            assert post_ddl[table] == [[NEW_REV]]
        elif table == 'image_allowlist':
            assert [row[:8] for row in post_ddl[table]] == pre_migration[table]
        else:
            assert post_ddl[table] == pre_migration[table], table
    run('06-not-backfilled-reject', 'authority_unavailable', new, db, 'AUTHORITY_NOT_BACKFILLED')
    run('07-backfill-conflicts', 'negative_backfills', new, db)
    run('08-backfill-export-orm', 'backfill', new, db)
    run('09-new-startup-gate', 'startup_check', new, db, 'accept', H1 + ',' + H2)
    ready = run('10-ready-backup', 'backup_ready', new, db)
    post_cutover = snapshot(db)
    restored = root / 'restored-a2' / 'moderation.db'
    restored.parent.mkdir()
    shutil.copyfile(ready['backup'], restored)
    shutil.copyfile(ready['json'], Path(str(restored) + '.rejections.json'))
    assert digest(restored) == ready['sha256']
    assert snapshot(restored) == post_cutover
    run('11-old-code-new-schema-reject', 'startup_check', old, restored, 'reject')
    run('12-bridge-new-schema-check', 'startup_check', bridge, restored, 'accept', H1 + ',' + H2)
    assert snapshot(restored) == post_cutover
    # R2 is deliberately separate from the kept-new-schema compatibility exercise.
    downgraded = root / 'r2-downgrade' / 'moderation.db'
    copy_database(restored, downgraded)
    shutil.copyfile(Path(str(restored) + '.rejections.json'), Path(str(downgraded) + '.rejections.json'))
    run('13-isolated-r2-downgrade', 'migrate', new, downgraded, 'downgrade', OLD_REV)
    run('14-r2-old-startup-gate', 'startup_check', old, downgraded, 'accept', H1 + ',' + H2)
    r2_snapshot = snapshot(downgraded)
    for table, rows in post_cutover.items():
        if table == 'alembic_version':
            assert r2_snapshot[table] == [[OLD_REV]]
        elif table == 'image_allowlist':
            assert r2_snapshot[table] == [row[:8] for row in rows]
        elif table == 'system_settings':
            assert r2_snapshot[table] == [row for row in rows if row[0] != 'image_decision_authority_version']
        else:
            assert r2_snapshot[table] == rows, table
    # The actual complete history is retained in the immutable pre-R2 A2 backup.
    assert snapshot(restored) == post_cutover
    restored_old = root / 'restored-pre-migration' / 'moderation.db'
    restored_old.parent.mkdir()
    shutil.copyfile(before_backup['backup'], restored_old)
    Path(str(restored_old) + '.rejections.json').write_bytes(initial_json)
    assert snapshot(restored_old) == pre_migration
    run('15-pre-migration-restore-check', 'startup_check', old, restored_old, 'accept', H1)
    integrity = {}
    for label, candidate in [('current', db), ('restored-a2', restored), ('r2', downgraded), ('restored-legacy', restored_old)]:
        with closing(sqlite3.connect(candidate)) as con:
            integrity[label] = {'integrity': con.execute('PRAGMA integrity_check').fetchall(), 'foreign_keys': con.execute('PRAGMA foreign_key_check').fetchall()}
            assert integrity[label] == {'integrity': [('ok',)], 'foreign_keys': []}
    results['final_invariants'] = {'ddl_preserved_legacy_data': True, 'r1_preserved_new_revision_and_history': True, 'r2_preserved_old_columns_and_other_tables': True, 'r2_full_authority_retained_in_a2_backup': True, 'pre_migration_restore_exact': True, 'integrity': integrity}
    write_json(root / 'summary.json', results)
    print(json.dumps({'run_directory': str(root), 'frozen_sha': sha, 'summary_sha256': digest(root / 'summary.json'), 'manifest_sha256': digest(root / 'manifest.json'), 'status': 'synthetic_acceptance_passed'}, indent=2))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        child(sys.argv[2], Path(sys.argv[3]).resolve(), Path(sys.argv[4]).resolve(), Path(sys.argv[5]).resolve(), sys.argv[6:])
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument('--sha', required=True)
        main(parser.parse_args().sha)
