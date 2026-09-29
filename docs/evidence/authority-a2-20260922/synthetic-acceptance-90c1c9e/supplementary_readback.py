import json, sqlite3, hashlib
from datetime import datetime, UTC
from pathlib import Path
from contextlib import closing
root=Path('C:/Users/<local-user>/AppData/Local/Temp/qqbot-a2-frozen-acceptance-7ae7d444ffc7/run-90c1c9e5d7cc-af07e34e')
manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
summary=json.loads((root/'summary.json').read_text(encoding='utf-8'))
started=datetime.fromisoformat(manifest['started_at_utc'])
now=datetime.now(UTC)
with closing(sqlite3.connect((root/'working/moderation.db').as_uri()+'?mode=ro',uri=True)) as con:
    con.row_factory=sqlite3.Row
    rows={row['phash']:dict(row) for row in con.execute('SELECT * FROM image_allowlist')}
    columns={row['name']:dict(row) for row in con.execute('PRAGMA table_info(image_allowlist)')}
    indexes=[dict(row) for row in con.execute('PRAGMA index_list(image_allowlist)')]
with closing(sqlite3.connect(Path(summary['02-synthetic-wal-backup']['result']['backup']).as_uri()+'?mode=ro',uri=True)) as con:
    con.row_factory=sqlite3.Row
    before={row['phash']:dict(row) for row in con.execute('SELECT * FROM image_allowlist')}
for key,prior in before.items():
    legacy=json.loads(rows[key]['history_json'])['legacy_row']
    assert {k:legacy[k] for k in prior}==prior
assert rows['0000000000000001']['decided_at'] is None
created=datetime.fromisoformat(rows['0000000000000003']['created_at'])
assert started <= created <= now
assert rows['0000000000000003']['decided_at'] is None
expected={'decision_state':('VARCHAR(16)',1,"''"),'decision_source':('VARCHAR(96)',1,"''"),'decision_operator':('VARCHAR(64)',1,"''"),'decision_version':('INTEGER',1,"'0'"),'decided_at':('DATETIME',0,None),'history_json':('TEXT',1,"'{}'")}
for name, expected_column in expected.items():
    row=columns[name]
    assert (row['type'],row['notnull'],row['dflt_value'])==expected_column,(name,row)
assert any(row['name']=='ix_image_allowlist_decision_state' for row in indexes)
result={'frozen_sha':manifest['frozen_sha'],'legacy_row_original_fields_preserved':True,'unknown_decision_time_null_for_existing_and_new_rows':True,'new_row_created_at_within_actual_run_window':True,'new_row_created_at':rows['0000000000000003']['created_at'],'migration_columns_exact':True,'decision_state_index_present':True}
(root/'supplementary-readback.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(result,indent=2))
print('result_sha256='+hashlib.sha256((root/'supplementary-readback.json').read_bytes()).hexdigest())
