import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { after, before, describe } from 'node:test';
import type { Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import type Database from 'better-sqlite3';
import type { getUsageSummary as GetUsageSummary, getUsageFacets as GetUsageFacets, getMonitorStats as GetMonitorStats, listMonitorSessions as ListMonitorSessions } from '../src/db/v2-queries.js';

let tempDir = '';
let closeDb: (() => void) | null = null;
let server: Server | null = null;
let baseUrl = '';
let getUsageSummary: typeof GetUsageSummary;
let getUsageFacets: typeof GetUsageFacets;
let getMonitorStats: typeof GetMonitorStats;
let listMonitorSessions: typeof ListMonitorSessions;
let getDb: () => Database.Database;

before(async () => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentmonitor-bench-seg-'));
  process.env.AGENTMONITOR_DB_PATH = path.join(tempDir, 'test.db');

  const { initSchema } = await import('../src/db/schema.js');
  const dbModule = await import('../src/db/connection.js');
  closeDb = dbModule.closeDb;
  getDb = dbModule.getDb;
  initSchema();
  assert.equal(getDb().name, path.join(tempDir, 'test.db'));

  const { insertEvent } = await import('../src/db/queries.js');
  ({ getUsageSummary, getUsageFacets, getMonitorStats, listMonitorSessions } = await import('../src/db/v2-queries.js'));

  // A real-activity event.
  insertEvent({
    event_id: 'real-001',
    session_id: 'real-sess',
    agent_type: 'claude_code',
    event_type: 'llm_response',
    status: 'success',
    project: 'alpha',
    model: 'claude-sonnet-4-5-20250929',
    tokens_in: 1000,
    tokens_out: 200,
    cost_usd: 0.5,
    source: 'api',
    metadata: null,
  });

  // A benchmark-import event, same metric shape.
  insertEvent({
    event_id: 'bench-001',
    session_id: 'codex:sometask:minimax-m3:trial1',
    agent_type: 'codex',
    event_type: 'llm_response',
    status: 'success',
    project: 'sometask',
    model: 'minimax-m3',
    tokens_in: 1000,
    tokens_out: 1000,
    cost_usd: 1.5,
    source: 'benchmark',
    metadata: null,
  });

  const { createApp } = await import('../src/app.js');
  const app = createApp({ serveStatic: false });
  server = app.listen(0);
  const addr = server.address() as AddressInfo;
  baseUrl = `http://127.0.0.1:${addr.port}`;
});

after(() => {
  server?.close();
  closeDb?.();
  fs.rmSync(tempDir, { recursive: true, force: true });
});

describe('benchmark usage segregation', () => {
  test('excludes benchmark events from the default usage summary', () => {
    const summary = getUsageSummary();
    assert.equal(summary.total_usage_events, 1, 'only the real event should count');
    assert.equal(summary.total_cost_usd, 0.5, 'benchmark cost must not sum in');
  });

  test('includes benchmark events when opted in', () => {
    const summary = getUsageSummary({ include_benchmark: true });
    assert.equal(summary.total_usage_events, 2);
    assert.equal(summary.total_cost_usd, 2.0);
  });

  test('facets honor the benchmark opt-in', () => {
    assert.ok(!getUsageFacets().models.includes('minimax-m3'), 'benchmark model absent by default');
    assert.ok(getUsageFacets({ include_benchmark: true }).models.includes('minimax-m3'), 'present when opted in');
  });
});

describe('benchmark Monitor segregation', () => {
  test('benchmark cost/tokens do not inflate Monitor stats', () => {
    const stats = getMonitorStats();
    assert.equal(stats.total_cost_usd, 0.5, 'benchmark cost excluded from Monitor totals');
    assert.equal(stats.total_events, 1, 'benchmark event excluded from Monitor event count');
    assert.ok(!('minimax-m3' in stats.model_breakdown), 'benchmark model absent from Monitor breakdown');
  });

  test('benchmark session is ended at insert and absent from the live session list', () => {
    const row = getDb()
      .prepare('SELECT status FROM sessions WHERE id = ?')
      .get('codex:sometask:minimax-m3:trial1') as { status: string } | undefined;
    assert.equal(row?.status, 'ended', 'benchmark session must not be live');

    const live = listMonitorSessions({ exclude_status: 'ended' });
    assert.ok(!live.sessions.some(s => s.id === 'codex:sometask:minimax-m3:trial1'), 'benchmark session absent from live list');
    assert.ok(live.sessions.some(s => s.id === 'real-sess'), 'real session present in live list');
  });
});

describe('benchmark opt-in through the usage HTTP API', () => {
  test('?include_benchmark=true reaches the query layer', async () => {
    const off = await (await fetch(`${baseUrl}/api/v2/usage/summary`)).json();
    assert.equal(off.total_cost_usd, 0.5, 'excluded by default over HTTP');

    const on = await (await fetch(`${baseUrl}/api/v2/usage/summary?include_benchmark=true`)).json();
    assert.equal(on.total_cost_usd, 2.0, 'opt-in flows through readAnalyticsParams');
  });
});
