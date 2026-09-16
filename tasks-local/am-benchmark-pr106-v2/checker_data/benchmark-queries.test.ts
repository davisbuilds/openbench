import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { after, before, describe } from 'node:test';
import type { Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import type { insertEvent as InsertEvent } from '../src/db/queries.js';
import type { getBenchmarkStudies as GetBenchmarkStudies, getBenchmarkStudy as GetBenchmarkStudy, getUsageSummary as GetUsageSummary } from '../src/db/v2-queries.js';
import type { BenchmarkStudySummary, BenchmarkStudyDetail } from '../src/api/v2/types.js';

let tempDir = '';
let closeDb: (() => void) | null = null;
let insertEvent: typeof InsertEvent;
let getBenchmarkStudies: typeof GetBenchmarkStudies;
let getBenchmarkStudy: typeof GetBenchmarkStudy;
let getUsageSummary: typeof GetUsageSummary;
let server: Server | null = null;
let baseUrl = '';

interface CellOpts {
  runId: string;
  study_id: string;
  study: string;
  suite?: string;
  canonical: string;
  effort?: string | null;
  is_open?: boolean | null;
  task: string;
  trial: number;
  score: number | null;
  cost: number | null;
  cost_source?: string | null;
  duration_ms?: number;
  cache_read?: number;
  success?: boolean;
  workspace_changed?: boolean | null;
  token_basis?: string | null;
  usage_evidence_grade?: string | null;
}

function cell(o: CellOpts): void {
  insertEvent({
    event_id: o.runId,
    session_id: o.runId,
    agent_type: 'codex',
    event_type: 'llm_response',
    status: o.success === false ? 'error' : 'success',
    project: o.task,
    model: o.effort ? `${o.canonical}-${o.effort}` : o.canonical,
    tokens_in: 100,
    tokens_out: 50,
    cache_read_tokens: o.cache_read ?? 0,
    cost_usd: o.cost,
    source: 'benchmark',
    study_id: o.study_id,
    study: o.study,
    duration_ms: o.duration_ms ?? 1000,
    metadata: {
      task: o.task,
      trial: o.trial,
      score: o.score,
      success: o.success ?? true,
      canonical_model: o.canonical,
      reasoning_effort: o.effort ?? null,
      is_open_model: o.is_open ?? null,
      suite: o.suite ?? null,
      cost_source: o.cost_source ?? null,
      workspace_changed: o.workspace_changed ?? null,
      token_basis: o.token_basis ?? null,
      usage_evidence_grade: o.usage_evidence_grade ?? null,
    },
  });
}

before(async () => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentmonitor-bench-q-'));
  process.env.AGENTMONITOR_DB_PATH = path.join(tempDir, 'test.db');

  const { initSchema } = await import('../src/db/schema.js');
  const dbModule = await import('../src/db/connection.js');
  closeDb = dbModule.closeDb;
  initSchema();
  assert.equal(dbModule.getDb().name, path.join(tempDir, 'test.db'));

  ({ insertEvent } = await import('../src/db/queries.js'));
  ({ getBenchmarkStudies, getBenchmarkStudy, getUsageSummary } = await import('../src/db/v2-queries.js'));

  // Study s1: four priced arms (frontier mechanics) + two honesty cases.
  // A cheap/routed (trivial), B mid/routed (value-pick, one noop), C native (n=1),
  // D bad/routed (dominated).
  cell({ runId: 's1-A-1', study_id: 'sha1', study: 's1-2026-09-03', suite: 's1', canonical: 'cheap', is_open: true, task: 't', trial: 1, score: 0.2, cost: 0.02, cost_source: null });
  cell({ runId: 's1-A-2', study_id: 'sha1', study: 's1-2026-09-03', suite: 's1', canonical: 'cheap', is_open: true, task: 't', trial: 2, score: 0.2, cost: 0.02, cost_source: null });
  cell({ runId: 's1-B-1', study_id: 'sha1', study: 's1-2026-09-03', suite: 's1', canonical: 'mid', is_open: true, task: 't', trial: 1, score: 0.8, cost: 0.10, cost_source: 'litellm' });
  cell({ runId: 's1-B-2', study_id: 'sha1', study: 's1-2026-09-03', suite: 's1', canonical: 'mid', is_open: true, task: 't', trial: 2, score: 0.8, cost: 0.10, cost_source: 'litellm', success: true, workspace_changed: false });
  cell({ runId: 's1-C-1', study_id: 'sha1', study: 's1-2026-09-03', suite: 's1', canonical: 'gpt-native', effort: 'high', is_open: false, task: 't', trial: 1, score: 1.0, cost: 0.50, cost_source: null });
  // A second gpt-native attempt that failed (unscored) — must count as an excluded
  // trial, not be hidden by the raw cell count. Keeps n=1, mean_score=1.0.
  cell({ runId: 's1-C-2', study_id: 'sha1', study: 's1-2026-09-03', suite: 's1', canonical: 'gpt-native', effort: 'high', is_open: false, task: 't', trial: 2, score: null, cost: 0.50, cost_source: null });
  cell({ runId: 's1-D-1', study_id: 'sha1', study: 's1-2026-09-03', suite: 's1', canonical: 'bad', is_open: true, task: 't', trial: 1, score: 0.5, cost: 0.60, cost_source: null });
  cell({ runId: 's1-D-2', study_id: 'sha1', study: 's1-2026-09-03', suite: 's1', canonical: 'bad', is_open: true, task: 't', trial: 2, score: 0.5, cost: 0.60, cost_source: null });

  // Study s2: a single unpriced arm.
  cell({ runId: 's2-E-1', study_id: 'sha2', study: 's2-2026-09-03', suite: 's2', canonical: 'mystery', is_open: true, task: 'u', trial: 1, score: 0.6, cost: null });

  // Study s3: native fallback when is_open_model is absent (legacy rows). A
  // first-party priced model → native; an unknown/unpriced model → routed.
  cell({ runId: 's3-F-1', study_id: 'sha3', study: 's3-2026-09-03', suite: 's3', canonical: 'gpt-5-mini', is_open: null, task: 'v', trial: 1, score: 0.9, cost: 0.10 });
  cell({ runId: 's3-G-1', study_id: 'sha3', study: 's3-2026-09-03', suite: 's3', canonical: 'no-such-model-xyz', is_open: null, task: 'v', trial: 1, score: 0.9, cost: null });

  // Study s4: a multi-task study (tasks p + q, trials 1 + 2 → four cells/arm).
  // Arm 'multi' is missing (q, trial 2), so it has 3 scored cells against an
  // expected grid of 4 — the excluded flag must catch it. The max-trial-index
  // denominator (2) would compute max(0, 2 - 3) = 0 and silently hide the gap.
  cell({ runId: 's4-M-p1', study_id: 'sha4', study: 's4-2026-09-03', suite: 's4', canonical: 'multi', is_open: true, task: 'p', trial: 1, score: 0.7, cost: 0.10, cost_source: 'litellm' });
  cell({ runId: 's4-M-p2', study_id: 'sha4', study: 's4-2026-09-03', suite: 's4', canonical: 'multi', is_open: true, task: 'p', trial: 2, score: 0.7, cost: 0.10, cost_source: 'litellm' });
  cell({ runId: 's4-M-q1', study_id: 'sha4', study: 's4-2026-09-03', suite: 's4', canonical: 'multi', is_open: true, task: 'q', trial: 1, score: 0.7, cost: 0.10, cost_source: 'litellm' });

  // A real-activity event that must never appear in benchmark queries or move Usage.
  insertEvent({
    event_id: 'real-1', session_id: 'real', agent_type: 'claude_code', event_type: 'llm_response',
    status: 'success', project: 'p', model: 'claude-sonnet-4-5-20250929',
    tokens_in: 1000, tokens_out: 200, cost_usd: 0.5, source: 'api', metadata: null,
  });

  const { createApp } = await import('../src/app.js');
  server = createApp({ serveStatic: false }).listen(0);
  baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

after(() => {
  server?.close();
  closeDb?.();
  fs.rmSync(tempDir, { recursive: true, force: true });
});

describe('getBenchmarkStudies', () => {
  test('lists studies with arm counts and cost basis, benchmark-only', () => {
    const studies = getBenchmarkStudies();
    assert.equal(studies.length, 4, 'exactly the benchmark studies (no real-activity)');

    const s1 = studies.find(s => s.study_id === 'sha1');
    assert.ok(s1);
    assert.equal(s1.study, 's1-2026-09-03');
    assert.equal(s1.suite, 's1');
    assert.equal(s1.arm_count, 4);
    assert.equal(s1.cell_count, 8);
    assert.equal(s1.cost_basis, 'derived', 'mixed captured/derived → derived');
    assert.ok(Math.abs((s1.total_cost_usd ?? -1) - (0.02 * 2 + 0.10 * 2 + 0.50 * 2 + 0.60 * 2)) < 1e-9);

    const s2 = studies.find(s => s.study_id === 'sha2');
    assert.ok(s2);
    assert.equal(s2.cost_basis, 'unpriced');
    assert.equal(s2.total_cost_usd, null, 'unpriced study totals to null');
  });
});

describe('getBenchmarkStudy — arm aggregation + frontier', () => {
  const armOf = (m: string) => getBenchmarkStudy('sha1').arms.find(a => a.canonical_model === m)!;

  test('aggregates trials into arms with mean score and cost/trial', () => {
    const b = armOf('mid');
    assert.equal(b.n, 2);
    assert.ok(Math.abs(b.mean_score - 0.8) < 1e-9);
    assert.ok(Math.abs((b.cost_per_trial ?? 0) - 0.10) < 1e-9);
    assert.equal(b.cost_basis, 'captured', 'all cells carry cost_source');
  });

  test('computes the Pareto frontier and verdicts', () => {
    assert.equal(armOf('cheap').pareto, true);
    assert.equal(armOf('cheap').verdict, 'trivial-only', 'on cost-frontier but below engage floor');
    assert.equal(armOf('mid').verdict, 'value-pick', 'best score-per-dollar among engaging arms');
    assert.equal(armOf('gpt-native').pareto, true);
    assert.equal(armOf('gpt-native').verdict, 'on-frontier');
    assert.equal(armOf('bad').pareto, false);
    assert.equal(armOf('bad').verdict, 'dominated');
    assert.equal(armOf('bad').dominated_by, 'mid', 'connector points to the cheapest arm that beats it');
  });

  test('derives native/hollow from is_open_model', () => {
    assert.equal(armOf('gpt-native').native, true, 'is_open_model false → native');
    assert.equal(armOf('mid').native, false, 'is_open_model true → routed');
  });

  test('native fallback (is_open_model absent): first-party → native, unknown → routed', () => {
    const arms = getBenchmarkStudy('sha3').arms;
    assert.equal(arms.find(a => a.canonical_model === 'gpt-5-mini')!.native, true, 'priced first-party → native');
    assert.equal(arms.find(a => a.canonical_model === 'no-such-model-xyz')!.native, false, 'unknown/unpriced → routed, never over-claim hollow');
  });

  test('surfaces honesty flags independent of verdict', () => {
    const detail = getBenchmarkStudy('sha1');
    assert.equal(detail.expected_trials, 2, 'one task × two trials = two cells/arm');
    assert.equal(armOf('gpt-native').n, 1, 'only the scored attempt counts toward n');
    assert.equal(armOf('gpt-native').excluded_trials, 1, 'the unscored 2nd attempt is an excluded trial, not hidden');
    assert.equal(armOf('mid').noop_trials, 1, 'one success with no workspace change');
    assert.equal(armOf('cheap').noop_trials, 0);
  });

  test('excluded_trials counts every task×trial cell, not just the max trial index', () => {
    const detail = getBenchmarkStudy('sha4');
    assert.equal(detail.expected_trials, 4, 'two tasks × two trials = four cells/arm');
    const m = detail.arms.find(a => a.canonical_model === 'multi')!;
    assert.equal(m.n, 3, 'three scored cells present');
    assert.equal(m.excluded_trials, 1, 'the missing (q, trial 2) cell is an excluded trial');
  });

  test('unpriced arm: cost/trial null, verdict unreliable, off the frontier', () => {
    const e = getBenchmarkStudy('sha2').arms.find(a => a.canonical_model === 'mystery')!;
    assert.equal(e.cost_per_trial, null);
    assert.equal(e.cost_basis, 'unpriced');
    assert.equal(e.pareto, false);
    assert.equal(e.verdict, 'unreliable');
  });
});

describe('benchmark segregation (read side)', () => {
  test('benchmark rows never leak into the default Usage summary', () => {
    const summary = getUsageSummary();
    assert.equal(summary.total_usage_events, 1, 'only the one real-activity event');
    assert.equal(summary.total_cost_usd, 0.5, 'benchmark cost excluded');
  });
});

describe('benchmark v2 routes', () => {
  test('GET /api/v2/benchmarks lists studies', async () => {
    const body = await (await fetch(`${baseUrl}/api/v2/benchmarks`)).json() as { data: BenchmarkStudySummary[] };
    assert.equal(body.data.length, 4);
    assert.ok(body.data.some(s => s.study_id === 'sha1'));
  });

  test('GET /api/v2/benchmarks/:studyId returns the arm detail', async () => {
    const detail = await (await fetch(`${baseUrl}/api/v2/benchmarks/sha1`)).json() as BenchmarkStudyDetail;
    assert.equal(detail.study_id, 'sha1');
    assert.equal(detail.arms.length, 4);
    assert.ok(detail.arms.some(a => a.verdict === 'value-pick'));
  });

  test('GET /api/v2/benchmarks/:studyId 404s an unknown study', async () => {
    const res = await fetch(`${baseUrl}/api/v2/benchmarks/nope`);
    assert.equal(res.status, 404);
  });
});
