"""Public observation driver, executed only inside the confined repair worker.

Schedules at ordinal database-read boundaries. It does not match SQL strings,
name a transaction API, or decide whether an observed state is correct.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

NODE = r'''
import fs from 'node:fs';
import path from 'node:path';
console.log=(...args)=>console.error(...args);
const [operation, database, targetText] = process.argv.slice(2);
process.env.AGENTMONITOR_DB_PATH=database;
const {getDb,closeDb}=await import('./src/db/connection.ts');
const schema=await import('./src/db/schema.ts');
const db=getDb();
if(path.resolve(db.name)!==path.resolve(database)) throw Error('wrong database');
const rawPragma=db.pragma.bind(db);
const before=rawPragma('foreign_keys',{simple:true});
const target=Number(targetText);
let reads=0;
const emit=value=>fs.writeSync(1,JSON.stringify(value)+'\n');
function observed(){
  reads++;
  if(reads===target){
    emit({event:'gate',ordinal:reads});
    if(fs.readSync(0,Buffer.alloc(1),0,1,null)!==1) throw Error('controller closed');
  }
}
db.pragma=(...args)=>{const value=rawPragma(...args);observed();return value;};
const prepare=db.prepare.bind(db);
db.prepare=(...args)=>{
  const statement=prepare(...args);
  if(statement.readonly){
    for(const method of ['get','all','run']){
      const original=statement[method].bind(statement);
      statement[method]=(...values)=>{const result=original(...values);observed();return result;};
    }
    const iterate=statement.iterate.bind(statement);
    statement.iterate=function*(...values){for(const row of iterate(...values)){observed();yield row;}};
  }
  return statement;
};
emit({event:'started'});
let error=null;
try {
  if(operation==='init') schema.initSchema();
  else if(operation==='migrate') schema.runDataMigrations(db);
  else if(operation==='read') schema.ensureSchemaForRead();
  else if(operation==='fk') schema.ensureTraceQualityExportStateFkFree(db);
  else throw Error('unknown operation');
} catch(e){error={code:String(e.code||''),message:String(e.message).slice(0,240)};}
const result={event:'done',reads,error,foreign_keys_before:before,
  foreign_keys_after:rawPragma('foreign_keys',{simple:true}),in_transaction:db.inTransaction};
closeDb();
emit(result);
'''


class Child:
    def __init__(self, root, operation, database, target=0):
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            ['node', '--import', '/opt/repair-deps/node_modules/tsx/dist/loader.mjs',
             'observe.mjs', operation, str(database), str(target)], cwd=root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
        self.buffer = b''
        self.events = []

    def event(self, timeout=8):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError('candidate event deadline')
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                return None
            self.buffer += chunk
            if len(self.buffer) > 65536:
                raise RuntimeError('candidate event limit')
        line, self.buffer = self.buffer.split(b'\n', 1)
        value = json.loads(line)
        self.events.append(value)
        return value

    def until(self, kind, timeout=8):
        deadline = time.monotonic() + timeout
        while True:
            value = self.event(max(.001, deadline - time.monotonic()))
            if value is None or value.get('event') == kind or value.get('event') == 'done':
                return value

    def release(self):
        if self.process.poll() is None:
            self.process.stdin.write(b'\n')
            self.process.stdin.flush()

    def finish(self, timeout=8):
        try:
            if not any(e.get('event') == 'done' for e in self.events):
                self.until('done', timeout)
            self.process.wait(timeout=timeout)
        except (TimeoutError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=3)
        self.stderr.seek(0)
        error = self.stderr.read(2000).decode(errors='replace')
        done = next((e for e in self.events if e.get('event') == 'done'), None)
        return {'exit_code': self.process.returncode, 'result': done, 'stderr': error}

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=3)
        self.process.stdin.close()
        self.process.stdout.close()
        self.stderr.close()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def seed(database, sql):
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(sql)


def snapshot(database):
    with sqlite3.connect(database, timeout=0) as db:
        db.row_factory = sqlite3.Row
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        schema = {name: [row['name'] for row in db.execute('PRAGMA table_info('+quote(name)+')')] for name in tables}
        rows = {}
        for table, columns in {
            'browsing_sessions': 'id,agent,first_message',
            'events': 'event_id,tokens_in,cache_read_tokens,source,model',
            'preserved': '*', 'trace_quality_export_state': '*',
        }.items():
            if table in tables:
                rows[table] = [dict(row) for row in db.execute('SELECT '+columns+' FROM '+quote(table)+' ORDER BY 1')]
        return {'schema': schema, 'rows': rows,
                'version': db.execute('PRAGMA user_version').fetchone()[0],
                'integrity': [row[0] for row in db.execute('PRAGMA integrity_check')],
                'export_foreign_keys': [dict(row) for row in db.execute('PRAGMA foreign_key_list(trace_quality_export_state)')]}


def single(root, database, operation):
    child = Child(root, operation, database)
    try:
        return child.finish()
    finally:
        child.close()


def pair(root, database, operation, ordinal):
    children = []
    try:
        a = Child(root, operation, database, ordinal)
        children.append(a)
        gate = a.until('gate') if ordinal else a.until('started')
        b = Child(root, operation, database)
        children.append(b)
        b.until('started')
        ownership = sqlite3.connect(database, timeout=0)
        try:
            try:
                ownership.execute('BEGIN IMMEDIATE')
                ownership.rollback()
                write_slot = 'free'
            except sqlite3.OperationalError as error:
                if 'locked' not in str(error):
                    raise
                write_slot = 'held'
        finally:
            ownership.close()
        order = 'no_gate' if not gate or gate.get('event') != 'gate' else 'sqlite_owner_released'
        if gate and gate.get('event') == 'gate' and write_slot == 'free':
            try:
                ended = b.until('done', timeout=1)
                order = 'b_completed_before_a_release' if ended and ended.get('event') == 'done' else 'b_exited_before_a_release'
            except TimeoutError:
                # Another valid synchronization strategy may own an external
                # lock. This grace only releases a schedule; it never scores it.
                order = 'contender_wait_released'
        a.release()
        return {'ordinal': ordinal, 'gate_observed': bool(gate and gate.get('event') == 'gate'),
                'write_slot': write_slot, 'order': order, 'workers': [a.finish(), b.finish()]}
    finally:
        for child in children:
            child.close()


def run_case(root, case):
    schemas = {}
    def observe(database):
        result = snapshot(database)
        schema = result.pop('schema')
        key = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
        schemas[key] = schema
        result['schema_key'] = key
        return result

    observations = []
    with tempfile.TemporaryDirectory(prefix='case-') as temporary:
        directory = Path(temporary)
        if case['kind'] == 'capture':
            database = directory/'capture.db'
            result = single(root, database, 'init')
            with sqlite3.connect(database) as db:
                ddl = ';\n'.join(row[0] for row in db.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name"))+';\nPRAGMA user_version=7;\n'
            return {'worker': result, 'ddl': ddl, 'state': snapshot(database)}
        if case['kind'] == 'sweep':
            database = directory/'trace.db'
            seed(database, case['sql'])
            trace = single(root, database, case['operation'])
            observations.append({'ordinal': 'serial', 'workers': [trace], 'state': observe(database)})
            count = (trace.get('result') or {}).get('reads', 0)
            if not 0 <= count <= 64:
                raise RuntimeError('read-boundary sweep exceeds development budget')
            for ordinal in range(1, count+1) if count else [0]:
                database = directory/f'pair-{ordinal}.db'
                seed(database, case['sql'])
                result = pair(root, database, case['operation'], ordinal)
                result['state'] = observe(database)
                result['reopen'] = single(root, database, case['operation'])
                result['reopened_state'] = observe(database)
                observations.append(result)
                for suffix in ('', '-wal', '-shm'):
                    Path(str(database)+suffix).unlink(missing_ok=True)
        elif case['kind'] == 'read':
            database = directory/'read.db'
            seed(database, case['sql'])
            writer = sqlite3.connect(database, timeout=0)
            try:
                writer.execute('BEGIN IMMEDIATE')
                observations.append({'writer_held': True, 'workers': [single(root, database, 'read')], 'state': observe(database)})
            finally:
                writer.rollback()
                writer.close()
        elif case['kind'] == 'rollback':
            database = directory/'rollback.db'
            seed(database, case['sql'])
            observations.append({'phase': 'injected_failure', 'workers': [single(root, database, 'migrate')], 'state': observe(database)})
            with sqlite3.connect(database) as db:
                db.execute('DROP TRIGGER fail_second_correction')
            observations.append({'phase': 'retry', 'workers': [single(root, database, 'migrate')], 'state': observe(database)})
        else:
            raise RuntimeError('unknown case kind')
    return {'schemas': schemas, 'observations': observations}


def main():
    request = json.loads(sys.stdin.buffer.read(4194305))
    with tempfile.TemporaryDirectory(prefix='candidate-') as directory:
        root = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(request['source']))) as archive:
            archive.extractall(root, filter='data')
        (root/'package.json').write_text('{"type":"module"}')
        (root/'node_modules').symlink_to('/opt/repair-deps/node_modules', target_is_directory=True)
        (root/'observe.mjs').write_text(NODE)
        results = [{'ok': True, 'value': run_case(root, case)} for case in request['cases']]
        print(json.dumps({'schema': 1, 'results': results}))


if __name__ == '__main__':
    main()
