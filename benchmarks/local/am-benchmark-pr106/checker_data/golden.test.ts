import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { before, beforeEach, after } from 'node:test';

const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'obench-am106-'));
const database = path.join(temp, 'fixture.db');
process.env.AGENTMONITOR_DB_PATH = database;
let db: any;
let closeDb: any;
let initSchema: any;
let runDataMigrations: any;
let insertEvent: any;
let importBenchmarkResults: any;
let getBenchmarkStudy: any;
let getBenchmarkStudies: any;
let getUsageSummary: any;

before(async () => {
  const connection = await import('../src/db/connection.js');
  closeDb = connection.closeDb;
  ({ initSchema, runDataMigrations } = await import('../src/db/schema.js'));
  ({ insertEvent } = await import('../src/db/queries.js'));
  ({ importBenchmarkResults } = await import('../src/import/benchmark.js'));
  ({ getBenchmarkStudy, getBenchmarkStudies, getUsageSummary } = await import('../src/db/v2-queries.js'));
  db = connection.getDb;
});
beforeEach(() => {
  closeDb();
  for (const ext of ['', '-wal', '-shm']) fs.rmSync(database + ext, { force: true });
  assert.equal(path.resolve(db().name), database);
  initSchema();
});
after(() => { closeDb?.(); fs.rmSync(temp, { recursive: true, force: true }); });

function row(overrides: Record<string, unknown> = {}) {
  return { run_id: 'codex:parser:gold-model:trial1', harness: 'codex', model: 'gold-model',
    canonical_model: 'gold-model', task: 'parser', trial: 1, score: 0.75, success: true,
    cost_usd: 0.25, cost_source: 'captured', tokens_input_uncached: 100,
    tokens_output: 20, study: 'shared-slug', study_sha256: 'study-A', ...overrides };
}
let fileCounter = 0;
function file(rows: object[], dir = temp) {
  fs.mkdirSync(dir, { recursive: true });
  const f = path.join(dir, `cells-${++fileCounter}.jsonl`);
  fs.writeFileSync(f, rows.map(r => JSON.stringify(r)).join('\n') + '\n');
  return f;
}
function benchRows() {
  return db().prepare("SELECT event_id, session_id, study_id, study, tokens_in, cost_usd, metadata FROM events WHERE source='benchmark' ORDER BY study_id, event_id").all();
}
function cell(study: string, model: string, taskName: string, trial: number, score: number | null) {
  insertEvent({ event_id: `${study}-${model}-${taskName}-${trial}`, session_id: `${study}-${model}`,
    agent_type: 'codex', event_type: 'llm_response', status: 'success', source: 'benchmark', study_id: study,
    study, project: taskName, model, cost_usd: 0.25, tokens_in: 100, tokens_out: 20,
    metadata: { canonical_model: model, task: taskName, trial, score, success: score !== null,
      cost_source: 'captured', is_open_model: true } });
}
function liveEvent() {
  insertEvent({ event_id: 'live-control', session_id: 'live-control', agent_type: 'codex',
    event_type: 'llm_response', status: 'success', source: 'api', tokens_in: 7, tokens_out: 3, cost_usd: 2,
    metadata: {} });
}

test('ID1 study reruns retain equal cell ids while repeat import is idempotent', () => {
  const a = file([row()]);
  const b = file([row({study_sha256: 'study-B', cost_usd: 0.5})]);
  assert.equal(importBenchmarkResults(a).eventsImported, 1);
  assert.equal(importBenchmarkResults(b).eventsImported, 1);
  assert.equal(importBenchmarkResults(a).duplicates, 1);
  assert.equal(importBenchmarkResults(b).duplicates, 1);
  const rows = benchRows();
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((r: any) => r.study_id), ['study-A', 'study-B']);
  assert.equal(new Set(rows.map((r: any) => r.session_id)).size, 2);
  for (const r of rows) assert.equal(JSON.parse(r.metadata).run_id, row().run_id);
  assert.equal(getBenchmarkStudies().length, 2);
});

test('ID2 equivalent absolute bare and parent-relative paths retain one legacy study', () => {
  const oldCwd = process.cwd();
  const directory = path.join(temp, 'nightly-study');
  const input = file([row({study_sha256: undefined, study: undefined})], directory);
  fs.mkdirSync(path.join(directory, 'nested'), {recursive: true});
  try {
    assert.equal(importBenchmarkResults(input).eventsImported, 1);
    process.chdir(directory);
    assert.equal(importBenchmarkResults(path.basename(input)).duplicates, 1);
    process.chdir(path.join(directory, 'nested'));
    assert.equal(importBenchmarkResults('../' + path.basename(input)).duplicates, 1);
  } finally { process.chdir(oldCwd); }
  const rows = benchRows();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].study_id, 'nightly-study');
});

test('ID3 relative paths in different directories retain different legacy studies', () => {
  const oldCwd = process.cwd();
  try {
    for (const name of ['march-run', 'april-run']) {
      const dir = path.join(temp, name);
      fs.mkdirSync(dir, {recursive: true});
      fs.writeFileSync(path.join(dir, 'results.jsonl'), JSON.stringify(row({study_sha256: undefined, study: undefined})) + '\n');
      process.chdir(dir);
      assert.equal(importBenchmarkResults('results.jsonl').eventsImported, 1);
    }
  } finally { process.chdir(oldCwd); }
  assert.deepEqual(benchRows().map((r: any) => r.study_id), ['april-run', 'march-run']);
});

test('ID4 cost backfill is scoped to one study and never overwrites captured cost', () => {
  const base = row({model: 'unpriced-obench-fictitious', canonical_model: 'unpriced-obench-fictitious', cost_usd: null});
  importBenchmarkResults(file([base]));
  importBenchmarkResults(file([{...base, study_sha256: 'study-B', cost_usd: 2}]));
  assert.equal(importBenchmarkResults(file([{...base, cost_usd: 0.75}])).costsBackfilled, 1);
  assert.equal(importBenchmarkResults(file([{...base, cost_usd: 9}])).costsBackfilled, 0);
  assert.deepEqual(benchRows().map((r: any) => [r.study_id, r.cost_usd]), [['study-A', 0.75], ['study-B', 2]]);
});

test('MIG1 pre-release orphan cleanup preserves modern and live accounting after reimport', () => {
  liveEvent();
  cell('modern-study', 'modern', 'parser', 1, 1);
  insertEvent({event_id: row().run_id, session_id: row().run_id, agent_type: 'codex',
    event_type: 'llm_response', status: 'success', source: 'benchmark', cost_usd: 0.25, tokens_in: 100,
    tokens_out: 20, metadata: {run_id: row().run_id}});
  db().pragma('user_version = 4');
  runDataMigrations(db());
  assert.equal(db().prepare("SELECT COUNT(*) n FROM events WHERE source='benchmark' AND study_id IS NULL").get().n, 0);
  const input = file([row()]);
  assert.equal(importBenchmarkResults(input).eventsImported, 1);
  assert.equal(importBenchmarkResults(input).duplicates, 1);
  assert.equal(benchRows().length, 2);
  assert.equal(benchRows().reduce((n: number, r: any) => n + r.cost_usd, 0), 0.5);
  assert.equal(getUsageSummary().total_cost_usd, 2);
  assert.equal(getUsageSummary({include_benchmark: true}).total_cost_usd, 2.5);
  runDataMigrations(db());
  assert.equal(benchRows().length, 2, 'restart must preserve imported rows');
});

test('MIG2 restrictive legacy table upgrade retains study metadata and accepts new event types', () => {
  assert.equal(path.resolve(db().name), database);
  db().exec(`DROP TABLE events;
    CREATE TABLE events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE, schema_version INTEGER NOT NULL DEFAULT 1,
      session_id TEXT NOT NULL, agent_type TEXT NOT NULL,
      event_type TEXT NOT NULL CHECK (event_type IN ('tool_use', 'response', 'error')),
      tool_name TEXT, status TEXT NOT NULL DEFAULT 'success', tokens_in INTEGER DEFAULT 0,
      tokens_out INTEGER DEFAULT 0, branch TEXT, project TEXT, duration_ms INTEGER,
      created_at TEXT NOT NULL DEFAULT (datetime('now')), metadata TEXT DEFAULT '{}',
      source TEXT DEFAULT 'api', study_id TEXT, study TEXT);
    INSERT INTO events (event_id,session_id,agent_type,event_type,source,study_id,study,tokens_in)
    VALUES ('retained','retained','codex','response','benchmark','existing-study','existing-slug',99);
  `);
  initSchema();
  const retained = db().prepare("SELECT study_id, study, tokens_in FROM events WHERE event_id='retained'").get();
  assert.deepEqual(retained, {study_id:'existing-study', study:'existing-slug', tokens_in:99});
  assert.doesNotThrow(() => insertEvent({event_id:'new-type',session_id:'new-type',agent_type:'codex',event_type:'session_update',status:'success',source:'api',metadata:{}}));
  initSchema();
  assert.equal(db().prepare('SELECT COUNT(*) n FROM events').get().n, 2);
});

test('GRID1 multi-task missing cells and unscored attempts count against the complete grid', () => {
  for (const taskName of ['parser', 'database']) for (const trial of [1, 2]) cell('grid', 'complete', taskName, trial, 1);
  cell('grid', 'partial', 'parser', 1, 0.8);
  cell('grid', 'partial', 'parser', 2, null);
  cell('grid', 'partial', 'database', 1, 0.6);
  const detail = getBenchmarkStudy('grid');
  assert.equal(detail.expected_trials, 4);
  const partial = detail.arms.find((a: any) => a.canonical_model === 'partial');
  const complete = detail.arms.find((a: any) => a.canonical_model === 'complete');
  assert.equal(partial.n, 2);
  assert.equal(partial.mean_score, 0.7);
  assert.equal(partial.excluded_trials, 2);
  assert.equal(complete.excluded_trials, 0);
  assert.equal(complete.n, 4);
});

test('GRID2 one task still counts unscored failures but zero scores are valid observations', () => {
  cell('single', 'failed', 'parser', 1, 0);
  cell('single', 'failed', 'parser', 2, null);
  const arm = getBenchmarkStudy('single').arms[0];
  assert.equal(arm.n, 1);
  assert.equal(arm.mean_score, 0);
  assert.equal(arm.excluded_trials, 1);
});

test('GUARD1 default usage excludes benchmark rows and optional usage includes their exact totals', () => {
  liveEvent();
  importBenchmarkResults(file([row(), row({run_id: 'another-cell', trial:2})]));
  assert.equal(getUsageSummary().total_usage_events, 1);
  assert.equal(getUsageSummary().total_cost_usd, 2);
  const all = getUsageSummary({include_benchmark: true});
  assert.equal(all.total_usage_events, 3);
  assert.equal(all.total_cost_usd, 2.5);
  assert.equal(getBenchmarkStudies().length, 1);
});

test('GUARD2 manual study overrides and faithful metadata survive repeat import', () => {
  const input = file([row({reasoning_effort: 'high', is_open_model: true})]);
  assert.equal(importBenchmarkResults(input, {study: 'manual'}).eventsImported, 1);
  assert.equal(importBenchmarkResults(input, {study: 'manual'}).duplicates, 1);
  const stored = benchRows();
  assert.equal(stored.length, 1);
  assert.equal(stored[0].study_id, 'manual');
  assert.equal(stored[0].study, 'manual');
  const meta = JSON.parse(stored[0].metadata);
  assert.equal(meta.canonical_model, 'gold-model');
  assert.equal(meta.reasoning_effort, 'high');
  assert.equal(meta.is_open_model, true);
});
