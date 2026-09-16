import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { after, before, describe } from 'node:test';
import type Database from 'better-sqlite3';
import type { importBenchmarkResults as ImportBenchmarkResults } from '../src/import/benchmark.js';
import type { getEvents as GetEvents, insertEvent as InsertEvent } from '../src/db/queries.js';
import type { runDataMigrations as RunDataMigrations } from '../src/db/schema.js';

let tempDir = '';
let closeDb: (() => void) | null = null;
let importBenchmarkResults: typeof ImportBenchmarkResults;
let getEvents: typeof GetEvents;
let insertEvent: typeof InsertEvent;
let runDataMigrations: typeof RunDataMigrations;
let getDb: () => Database.Database;

function writeResults(rows: object[]): string {
  const file = path.join(tempDir, `results-${Math.random().toString(36).slice(2)}.jsonl`);
  fs.writeFileSync(file, rows.map(r => JSON.stringify(r)).join('\n') + '\n');
  return file;
}

// Write results.jsonl into a named study directory, so the legacy parent-dir
// derivation has a deterministic name to fall back to.
function writeResultsInDir(dirName: string, rows: object[]): string {
  const dir = path.join(tempDir, dirName);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 'results.jsonl');
  fs.writeFileSync(file, rows.map(r => JSON.stringify(r)).join('\n') + '\n');
  return file;
}

// Persisted event_id is namespaced `${study_id}::${run_id}`, so resolve the row
// by the run_id suffix (unique within these single-study fixtures).
function benchRow(runId: string): { event_id: string; study_id: string | null; study: string | null; metadata: string } {
  const rows = getDb()
    .prepare("SELECT event_id, study_id, study, metadata FROM events WHERE source = 'benchmark'")
    .all() as Array<{ event_id: string; study_id: string | null; study: string | null; metadata: string }>;
  const hit = rows.find(r => r.event_id.endsWith(`::${runId}`));
  if (!hit) throw new Error(`no benchmark row for run_id ${runId}`);
  return hit;
}

function studyCols(runId: string): { study_id: string | null; study: string | null } {
  const { study_id, study } = benchRow(runId);
  return { study_id, study };
}

function meta(runId: string): Record<string, unknown> {
  return JSON.parse(benchRow(runId).metadata) as Record<string, unknown>;
}

const minimaxRow = {
  run_id: 'codex:task-a:minimax-m3:trial1',
  harness: 'codex',
  model: 'minimax-m3',
  task: 'task-a',
  trial: 1,
  ts_iso: '2026-08-29T14:18:20',
  success: true,
  completed: true,
  wall_time_s: 12.5,
  turns: 3,
  tokens_input_uncached: 1_000_000,
  tokens_output: 1_000_000,
  tokens_cache_read: 0,
  tokens_cache_write: 0,
  tokens_reasoning: 200,
  token_basis: 'vendor_split',
  cost_usd: null,
};

// codex daily-driver comparator with a reasoning-effort suffix on the model.
const codexRow = {
  run_id: 'codex:task-a:gpt-5.6-terra-xhigh:trial1',
  harness: 'codex',
  model: 'gpt-5.6-terra-xhigh',
  task: 'task-a',
  trial: 1,
  ts_iso: '2026-08-29T14:20:00',
  success: true,
  tokens_input_uncached: 100_000,
  tokens_output: 1_000,
};

before(async () => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentmonitor-bench-import-'));
  process.env.AGENTMONITOR_DB_PATH = path.join(tempDir, 'test.db');

  const { initSchema } = await import('../src/db/schema.js');
  const dbModule = await import('../src/db/connection.js');
  closeDb = dbModule.closeDb;
  getDb = dbModule.getDb;
  initSchema();
  assert.equal(dbModule.getDb().name, path.join(tempDir, 'test.db'));

  ({ importBenchmarkResults } = await import('../src/import/benchmark.js'));
  ({ getEvents, insertEvent } = await import('../src/db/queries.js'));
  ({ runDataMigrations } = await import('../src/db/schema.js'));
});

after(() => {
  closeDb?.();
  fs.rmSync(tempDir, { recursive: true, force: true });
});

describe('importBenchmarkResults', () => {
  test('imports rows as segregated benchmark events with derived cost', () => {
    const file = writeResults([minimaxRow, codexRow]);
    const result = importBenchmarkResults(file);

    assert.equal(result.rowsRead, 2);
    assert.equal(result.eventsImported, 2);
    assert.equal(result.duplicates, 0);
    assert.deepEqual(result.unpricedModels, []);

    const events = getEvents({ source: 'benchmark' }).events;
    assert.equal(events.length, 2);

    const minimax = events.find(e => e.model === 'minimax-m3');
    assert.ok(minimax);
    assert.equal(minimax.source, 'benchmark');
    // event/session id is namespaced `${study_id}::${run_id}`; the faithful run_id
    // is preserved in metadata.
    assert.ok(minimax.session_id.endsWith('::codex:task-a:minimax-m3:trial1'));
    assert.equal(JSON.parse(minimax.metadata as string).run_id, 'codex:task-a:minimax-m3:trial1');
    // 1M input * 0.30/MTok + 1M output * 1.20/MTok
    assert.ok(Math.abs((minimax.cost_usd ?? 0) - 1.5) < 1e-9, `got ${minimax.cost_usd}`);

    // Effort-suffix stripped so the codex base model resolves to a real price.
    const codex = events.find(e => e.model === 'gpt-5.6-terra-xhigh');
    assert.ok(codex);
    assert.ok((codex.cost_usd ?? 0) > 0, 'effort-suffixed codex model should be priced');
  });

  test('is idempotent — re-importing the same file inserts nothing', () => {
    const file = writeResults([minimaxRow]);
    importBenchmarkResults(file);
    const second = importBenchmarkResults(file);
    assert.equal(second.eventsImported, 0);
    assert.equal(second.duplicates, 1);
  });

  test('reports unpriced models loudly and skips malformed rows', () => {
    const file = writeResults([
      { run_id: 'codex:task-b:mystery-model:trial1', harness: 'codex', model: 'mystery-model', tokens_input_uncached: 500, tokens_output: 100 },
      { run_id: 'no-model', harness: 'codex' }, // missing model -> skipped
    ]);
    const result = importBenchmarkResults(file);
    assert.equal(result.skipped, 1);
    assert.deepEqual(result.unpricedModels, ['mystery-model']);
  });

  test('dry-run counts without writing', () => {
    const file = writeResults([{ ...minimaxRow, run_id: 'codex:task-c:minimax-m3:trial1' }]);
    const before = getEvents({ source: 'benchmark' }).events.length;
    const result = importBenchmarkResults(file, { dryRun: true });
    assert.equal(result.eventsImported, 1);
    assert.equal(getEvents({ source: 'benchmark' }).events.length, before);
  });

  test('does not project codex benchmark cells into the live session browser', () => {
    const runId = 'codex:task-live:minimax-m3:trial1';
    const file = writeResults([{ ...minimaxRow, run_id: runId }]);
    importBenchmarkResults(file);

    const browsing = getDb()
      .prepare('SELECT COUNT(*) AS n FROM browsing_sessions WHERE id = ?')
      .get(runId) as { n: number };
    assert.equal(browsing.n, 0, 'codex benchmark row must not create a browsing_sessions projection');
  });

  test('re-import backfills a benchmark cost that was null at first import', () => {
    const runId = 'codex:task-backfill:mystery-model:trial1';
    // First import: model unpriced and the row carries no cost -> stored as null.
    const first = writeResults([
      { run_id: runId, harness: 'codex', model: 'mystery-model', tokens_input_uncached: 500, tokens_output: 100, cost_usd: null },
    ]);
    importBenchmarkResults(first);
    const stored = getEvents({ source: 'benchmark' }).events.find(e => e.session_id.endsWith(`::${runId}`));
    assert.ok(stored);
    assert.equal(stored.cost_usd, null, 'cost is null while the model is unpriced');

    // Re-run once a cost resolves (rates added / row now carries cost_usd).
    const second = writeResults([
      { run_id: runId, harness: 'codex', model: 'mystery-model', tokens_input_uncached: 500, tokens_output: 100, cost_usd: 0.42 },
    ]);
    const result = importBenchmarkResults(second);
    assert.equal(result.duplicates, 1, 'the run_id already exists');
    assert.equal(result.costsBackfilled, 1, 'the previously-null cost is backfilled');

    const refreshed = getEvents({ source: 'benchmark' }).events.find(e => e.session_id.endsWith(`::${runId}`));
    assert.ok(Math.abs((refreshed?.cost_usd ?? 0) - 0.42) < 1e-9, `expected backfilled 0.42, got ${refreshed?.cost_usd}`);
  });

  test('reads study + model identity straight from openbench row fields', () => {
    const runId = 'codex:task-id:gpt-5.6-terra-xhigh:trial1';
    const file = writeResults([{
      run_id: runId, harness: 'codex', model: 'gpt-5.6-terra-xhigh',
      task: 'task-id', trial: 1, tokens_input_uncached: 1000, tokens_output: 100,
      canonical_model: 'gpt-5.6-terra', reasoning_effort: 'xhigh', is_open_model: false,
      study: 'my-suite-2026-09-03', study_sha256: 'sha-abc123', suite: 'my-suite',
    }]);
    importBenchmarkResults(file);

    const cols = studyCols(runId);
    assert.equal(cols.study_id, 'sha-abc123', 'study_id = openbench study_sha256 (grouping key)');
    assert.equal(cols.study, 'my-suite-2026-09-03', 'study slug is the display label');

    const m = meta(runId);
    assert.equal(m.canonical_model, 'gpt-5.6-terra');
    assert.equal(m.reasoning_effort, 'xhigh');
    assert.equal(m.is_open_model, false);
    assert.equal(m.suite, 'my-suite');
  });

  test('legacy fallback: no openbench identity fields → derive from parent dir + strip suffix', () => {
    const runId = 'codex:legacy:minimax-m3:trial1';
    // A pre-field row: no study/study_sha256/canonical_model/reasoning_effort.
    const file = writeResultsInDir('legacy-study-2026-09-03', [{
      run_id: runId, harness: 'codex', model: 'gpt-5.6-terra-xhigh',
      task: 'legacy', trial: 1, tokens_input_uncached: 1000, tokens_output: 100,
    }]);
    importBenchmarkResults(file);

    const cols = studyCols(runId);
    assert.equal(cols.study_id, 'legacy-study-2026-09-03', 'legacy study_id falls back to parent dir');
    assert.equal(cols.study, 'legacy-study-2026-09-03', 'legacy slug falls back to parent dir');

    const m = meta(runId);
    assert.equal(m.canonical_model, 'gpt-5.6-terra', 'effort suffix stripped as legacy fallback');
    assert.equal(m.reasoning_effort, 'xhigh', 'recognized suffix recovered');
  });

  test('bare/relative path resolves to its real containing directory, not "." or ".."', () => {
    const runId = 'codex:bare:minimax-m3:trial1';
    const prevCwd = process.cwd();
    try {
      process.chdir(tempDir);
      fs.writeFileSync('bare.jsonl', JSON.stringify({ run_id: runId, harness: 'codex', model: 'minimax-m3', task: 'bare', trial: 1, tokens_input_uncached: 10, tokens_output: 5 }) + '\n');
      // A bare or parent-relative path must resolve to the real directory the
      // file lives in — never the literal '.' or '..' segment, which would merge
      // unrelated runs under one meaningless study key.
      importBenchmarkResults('bare.jsonl');
    } finally {
      process.chdir(prevCwd);
    }
    assert.equal(studyCols(runId).study_id, path.basename(tempDir), 'resolves to the containing dir name');
  });

  test('data migration deletes pre-namespacing benchmark rows (study_id NULL) so re-import cannot double-count', () => {
    const db = getDb();
    // A legacy benchmark row: keyed on the bare run_id, no study_id column set.
    // Re-importing its file now inserts a fresh `${study_id}::${run_id}` row, so
    // the orphan must be removed or include_benchmark usage double-counts it.
    insertEvent({
      event_id: 'legacy:run:1', session_id: 'legacy:run:1', agent_type: 'codex',
      event_type: 'llm_response', status: 'success', model: 'minimax-m3',
      tokens_in: 10, tokens_out: 5, cost_usd: 0.01, source: 'benchmark', metadata: null,
    });
    insertEvent({
      event_id: 'sha-keep::run:1', session_id: 'sha-keep::run:1', agent_type: 'codex',
      event_type: 'llm_response', status: 'success', model: 'minimax-m3',
      tokens_in: 10, tokens_out: 5, cost_usd: 0.01, source: 'benchmark',
      study_id: 'sha-keep', study: 'keep', metadata: null,
    });
    const nullRows = (): number =>
      (db.prepare("SELECT COUNT(*) AS n FROM events WHERE source = 'benchmark' AND study_id IS NULL").get() as { n: number }).n;
    assert.equal(nullRows(), 1, 'legacy NULL-study benchmark row present before migration');

    // Re-run the versioned data migration from before the v5 cleanup.
    db.pragma('user_version = 4');
    runDataMigrations(db);

    assert.equal(nullRows(), 0, 'legacy NULL-study benchmark row deleted');
    const kept = (db.prepare("SELECT COUNT(*) AS n FROM events WHERE event_id = 'sha-keep::run:1'").get() as { n: number }).n;
    assert.equal(kept, 1, 'namespaced row preserved');
  });

  test('two runs of one suite with the same run_id are two studies (event_id namespaced)', () => {
    // openbench reruns produce the SAME run_id (harness:task:model:trial); only
    // study_sha256 differs. Keying events on run_id alone would drop run B.
    const runId = 'codex:t:glm-5.3-flash:trial1';
    const base = { run_id: runId, harness: 'codex', model: 'glm-5.3-flash', task: 't', trial: 1, tokens_input_uncached: 10, tokens_output: 5, study: 'dup-suite-2026-09-03', suite: 'dup-suite' };
    const a = importBenchmarkResults(writeResults([{ ...base, study_sha256: 'sha-runA' }]));
    const b = importBenchmarkResults(writeResults([{ ...base, study_sha256: 'sha-runB' }]));

    assert.equal(a.eventsImported, 1);
    assert.equal(b.eventsImported, 1, 'same run_id in a different study is not a duplicate');
    assert.equal(b.duplicates, 0);

    const byId = (eid: string) => getDb().prepare('SELECT study_id FROM events WHERE event_id = ?').get(eid) as { study_id: string } | undefined;
    assert.equal(byId(`sha-runA::${runId}`)?.study_id, 'sha-runA');
    assert.equal(byId(`sha-runB::${runId}`)?.study_id, 'sha-runB');
    const distinct = getDb().prepare(
      "SELECT COUNT(DISTINCT study_id) AS n FROM events WHERE study = 'dup-suite-2026-09-03'"
    ).get() as { n: number };
    assert.equal(distinct.n, 2, 'same slug, two grouping keys, both cells kept');
  });

  test('re-importing the same study is idempotent', () => {
    const row = { run_id: 'codex:idem:minimax-m3:trial1', harness: 'codex', model: 'minimax-m3', task: 'idem', trial: 1, tokens_input_uncached: 10, tokens_output: 5, study: 'idem-suite', study_sha256: 'sha-idem', suite: 'idem-suite' };
    assert.equal(importBenchmarkResults(writeResults([row])).eventsImported, 1);
    const second = importBenchmarkResults(writeResults([row]));
    assert.equal(second.eventsImported, 0);
    assert.equal(second.duplicates, 1, 'same study + run_id → duplicate');
  });

  test('skips non-object JSON rows instead of aborting the whole import', () => {
    const goodRow = { ...minimaxRow, run_id: 'codex:task-good:minimax-m3:trial1' };
    const file = path.join(tempDir, 'nonobject.jsonl');
    // `null`, an array, and a bare number are all valid JSON but not benchmark rows.
    fs.writeFileSync(file, ['null', '[1,2,3]', '42', JSON.stringify(goodRow)].join('\n') + '\n');
    const result = importBenchmarkResults(file);
    assert.equal(result.rowsRead, 4);
    assert.equal(result.skipped, 3, 'the three non-object rows are skipped');
    assert.equal(result.eventsImported, 1, 'the valid row still imports');
  });
});
