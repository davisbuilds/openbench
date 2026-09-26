"""Trusted development expectations for the #123 observation driver.

This module is not sent to candidate containers or registered for model trials.
"""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
MARKER = "CREATE TABLE preserved(id TEXT PRIMARY KEY,value TEXT); INSERT INTO preserved VALUES('keep','unchanged');\n"
OLD = '''CREATE TABLE browsing_sessions (
id TEXT PRIMARY KEY, project TEXT, agent TEXT NOT NULL,first_message TEXT,
started_at TEXT,ended_at TEXT,message_count INTEGER NOT NULL DEFAULT 0,
user_message_count INTEGER NOT NULL DEFAULT 0,parent_session_id TEXT,
relationship_type TEXT,file_path TEXT,file_size INTEGER,file_hash TEXT);
INSERT INTO browsing_sessions(id,agent,first_message) VALUES('keep','codex','preserve me');
'''
EVENTS = '''INSERT INTO events(event_id,session_id,agent_type,event_type,status,tokens_in,tokens_out,model,cost_usd,cache_read_tokens,cache_write_tokens,source)
VALUES ('first','s1','codex','llm_response','success',100000,0,'gpt-5.4',1.01,40000,0,'import'),
('second','s2','codex','llm_response','success',90000,0,'gpt-5.4',1.01,10000,0,'import'),
('anthropic','s3','claude','llm_response','success',12000,0,'claude-sonnet-4-6',1.01,2000,0,'import');
PRAGMA user_version=0;
'''


def cases():
    current = (ROOT/'current.sql').read_text()
    # This is an actual old table shape, not a current schema with a low marker.
    legacy_export = '''DROP TABLE trace_quality_export_state;
CREATE TABLE trace_quality_traces(id TEXT PRIMARY KEY);
CREATE TABLE trace_quality_observations(id TEXT PRIMARY KEY);
INSERT INTO trace_quality_traces VALUES('trace');
INSERT INTO trace_quality_observations VALUES('observation');
'''+next(part for part in current.split(';') if 'CREATE TABLE trace_quality_export_state ' in part).replace(
        'local_trace_id TEXT NOT NULL', 'local_trace_id TEXT NOT NULL REFERENCES trace_quality_traces(id)').replace(
        'local_observation_id TEXT', 'local_observation_id TEXT REFERENCES trace_quality_observations(id)')+''';
INSERT INTO trace_quality_export_state(id,provider,local_trace_id,local_observation_id,status,metadata_json,created_at)
VALUES(41,'langfuse','trace','observation','pending','{"keep":true}','2026-01-01 00:00:00');
'''
    values = [
        ('structure', {'kind':'sweep','operation':'init','sql':OLD+MARKER}),
        ('correction', {'kind':'sweep','operation':'migrate','sql':current+EVENTS+MARKER}),
        ('foreign_keys', {'kind':'sweep','operation':'init','sql':current+legacy_export+MARKER}),
        ('current_read', {'kind':'read','sql':current+MARKER}),
        ('legacy_read', {'kind':'sweep','operation':'read','sql':OLD+MARKER}),
        ('rollback', {'kind':'rollback','sql':current+EVENTS+MARKER+'''CREATE TRIGGER fail_second_correction BEFORE UPDATE OF tokens_in ON events
WHEN OLD.event_id='second' BEGIN SELECT RAISE(ABORT,'injected correction failure'); END;
'''}),
    ]
    return values


def workers_ok(workers):
    return bool(workers) and all(w.get('exit_code') == 0 and isinstance(w.get('result'),dict)
        and w['result'].get('error') is None and w['result'].get('foreign_keys_before') == 1
        and w['result'].get('foreign_keys_after') == 1 and w['result'].get('in_transaction') is False for w in workers)


def state_ok(state, schemas, *, version=7):
    expected = json.loads((ROOT/'current-schema.json').read_text())
    actual = schemas.get(state.get('schema_key'), {})
    return (all(set(columns) <= set(actual.get(table, [])) for table,columns in expected.items())
            and state.get('version') == version and state.get('integrity') == ['ok']
            and state.get('rows',{}).get('preserved') == [{'id':'keep','value':'unchanged'}])


def tokens(state):
    return {row['event_id']:row['tokens_in'] for row in state.get('rows',{}).get('events', [])}


def grade(name, value):
    observations = value.get('observations', [])
    schemas = value.get('schemas', {})
    if not observations:
        return False
    if name == 'rollback':
        if [row.get('phase') for row in observations] != ['injected_failure','retry']:
            return False
    elif name == 'current_read':
        if len(observations) != 1 or observations[0].get('writer_held') is not True:
            return False
    else:
        first = observations[0]
        workers = first.get('workers', [])
        count = (workers[0].get('result') or {}).get('reads') if len(workers) == 1 else None
        if type(count) is not int or not 0 <= count <= 64:
            return False
        expected = ['serial', *(range(1,count+1) if count else [0])]
        if [row.get('ordinal') for row in observations] != expected:
            return False
        if any(len(row.get('workers', [])) != 2 for row in observations[1:]):
            return False
    for row in observations:
        state = row.get('state', {})
        rollback = name == 'rollback' and row.get('phase') == 'injected_failure'
        if not state_ok(state, schemas, version=0 if rollback else 7):
            return False
        if rollback:
            workers = row.get('workers', [])
            if len(workers) != 1 or not (workers[0].get('result') or {}).get('error'):
                return False
            if tokens(state) != {'first':100000,'second':90000,'anthropic':12000}:
                return False
        elif not workers_ok(row.get('workers', [])):
            return False
        if name in ('current_read','legacy_read') and any(
                (worker.get('result') or {}).get('read_value') != {'id':'keep','value':'unchanged'}
                for worker in row.get('workers', [])):
            return False
        if name in ('structure','legacy_read') and state.get('rows',{}).get('browsing_sessions') != [
                {'id':'keep','agent':'codex','first_message':'preserve me'}]:
            return False
        if name in ('correction','rollback') and not rollback and tokens(state) != {'first':60000,'second':80000,'anthropic':12000}:
            return False
        if name == 'foreign_keys':
            exports = state.get('rows',{}).get('trace_quality_export_state', [])
            if state.get('export_foreign_keys') or len(exports) != 1:
                return False
            preserved={'id':41,'provider':'langfuse','local_trace_id':'trace','local_observation_id':'observation',
                       'status':'pending','metadata_json':'{"keep":true}','created_at':'2026-01-01 00:00:00'}
            if any(exports[0].get(key) != expected for key,expected in preserved.items()):
                return False
        if 'reopened_state' in row and (row['reopened_state'] != state or not workers_ok([row.get('reopen', {})])):
            return False
    return True


def variants(base, partial, reference):
    output = {'buggy':base,'historical-partial':partial,'reference':reference}
    start = reference.index('export function initSchema(): void {')
    end = reference.index('// Schema-version counter', start)
    output['optimistic-retry'] = reference[:start]+'''export function initSchema(): void {
  const db=getDb();
  for(let attempt=0;;attempt++) {
    try { ensureTraceQualityExportStateFkFree(db); initSchemaLocked(db); return; }
    catch(error) {
      const e=error as {code?:string,message?:string};
      if(attempt>=3 || !(e.code==='SQLITE_BUSY' || e.code==='SQLITE_LOCKED' ||
          /duplicate column name/i.test(e.message||''))) throw error;
    }
  }
}

'''+reference[end:]
    output['column-only-retry'] = output['optimistic-retry']
    output['optimistic-retry'] = output['optimistic-retry'].replace(
        '/duplicate column name/i', '/duplicate column name|(?:table|index|trigger) .+ already exists/i')
    explicit = reference[:start]+'''export function initSchema(): void {
  const db=getDb();
  ensureTraceQualityExportStateFkFree(db);
  db.exec('BEGIN IMMEDIATE');
  try { initSchemaLocked(db); db.exec('COMMIT'); }
  catch(error) { if(db.inTransaction) db.exec('ROLLBACK'); throw error; }
}

'''+reference[end:]
    explicit = explicit.replace("db.pragma('user_version', { simple: true })", "(db.prepare('PRAGMA USER_VERSION').get() as {user_version:number}).user_version")
    explicit = re.sub(r"db.prepare\('PRAGMA table_info\((\w+)\)'\)\.all\(\)",
                      lambda m:"db.prepare('SELECT * FROM pragma_table_info(?)').all('"+m[1]+"')", explicit)
    output['sql-api-alternative'] = explicit
    output['always-initialize'] = reference.replace('if (current >= DATA_SCHEMA_VERSION) return;\n  initSchema();','initSchema();',1)
    begin = reference.index('export function runDataMigrations')
    end = reference.index('\n/**',begin)
    section = reference[begin:end].replace('const run = db.transaction(() => {','const run = () => {').replace('  });\n  run.immediate();','  };\n  run();')
    output['non-atomic-migration'] = reference[:begin]+section+reference[end:]
    output['no-op-initializer'] = reference[:start]+'export function initSchema(): void {}\n\n'+reference[reference.index('// Schema-version counter',start):]
    if len(set(output.values())) != len(output):
        raise RuntimeError('a control mutation did not change its source')
    return output
