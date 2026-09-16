import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { after, before, describe } from 'node:test';

// Isolate the DB before importing any module that resolves the connection.
const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentmonitor-backfill-'));
process.env.AGENTMONITOR_DB_PATH = path.join(tempDir, 'agentmonitor.db');

const { initSchema, runDataMigrations } = await import('../src/db/schema.js');
const { closeDb, getDb } = await import('../src/db/connection.js');

function insertEventRow(row: {
  event_id: string;
  agent_type: string;
  model: string;
  tokens_in: number;
  cache_read_tokens: number;
  cost_usd: number;
}): void {
  getDb().prepare(`
    INSERT INTO events (event_id, session_id, agent_type, event_type, status,
      tokens_in, tokens_out, model, cost_usd, cache_read_tokens, cache_write_tokens, source)
    VALUES (?, 'sess', ?, 'llm_response', 'success', ?, 0, ?, ?, ?, 0, 'import')
  `).run(row.event_id, row.agent_type, row.tokens_in, row.model, row.cost_usd, row.cache_read_tokens);
}

function getRow(eventId: string): { tokens_in: number; cost_usd: number | null } {
  return getDb()
    .prepare('SELECT tokens_in, cost_usd FROM events WHERE event_id = ?')
    .get(eventId) as { tokens_in: number; cost_usd: number | null };
}

describe('cache-inclusive input backfill migration', () => {
  before(() => {
    initSchema();
  });

  after(() => {
    closeDb();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  test('re-normalizes OpenAI rows to net input and leaves Anthropic untouched', () => {
    const db = getDb();

    // Seed historical rows in the pre-fix (cache-inclusive) convention. gpt-5.4:
    // 100k inclusive input, 40k cached → old buggy cost billed all 100k at input.
    insertEventRow({
      event_id: 'openai-old', agent_type: 'codex', model: 'gpt-5.4',
      tokens_in: 100_000, cache_read_tokens: 40_000, cost_usd: 1.01,
    });
    // Anthropic input_tokens is already net; must not be altered.
    insertEventRow({
      event_id: 'anthropic-net', agent_type: 'claude_code', model: 'claude-opus-4-8',
      tokens_in: 1500, cache_read_tokens: 800, cost_usd: 0.0079,
    });

    // The migration ran once during initSchema (empty table). Reset the guard so
    // it re-runs over the seeded rows.
    db.pragma('user_version = 0');
    runDataMigrations(db);

    const openai = getRow('openai-old');
    assert.equal(openai.tokens_in, 60_000); // 100k - 40k cached
    // 60k*$2.5 + 40k*$0.25 per MTok = 0.15 + 0.01 = 0.16
    assert.ok(openai.cost_usd !== null && Math.abs(openai.cost_usd - 0.16) < 0.0001);

    const anthropic = getRow('anthropic-net');
    assert.equal(anthropic.tokens_in, 1500);
    assert.equal(anthropic.cost_usd, 0.0079);
  });

  test('version guard makes it run exactly once', () => {
    const db = getDb();
    const before = getRow('openai-old');
    // runDataMigrations advances the counter to the current DATA_SCHEMA_VERSION
    // (bumped to 4 when skill-context session backfill was added).
    assert.equal(db.pragma('user_version', { simple: true }), 5);

    // Re-running without resetting the guard must short-circuit — no second
    // subtraction of the already-netted cached tokens.
    runDataMigrations(db);

    const after = getRow('openai-old');
    assert.equal(after.tokens_in, before.tokens_in);
    assert.equal(after.cost_usd, before.cost_usd);
  });
});
