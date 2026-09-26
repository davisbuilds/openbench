import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { after, before } from 'node:test';

import type { closeDb as closeDbType, getDb as getDbType } from '../src/db/connection.js';
import type { initSchema as initSchemaType } from '../src/db/schema.js';

let tempDir = '';
let initSchema: typeof initSchemaType;
let closeDb: typeof closeDbType;
let getDb: typeof getDbType;

before(async () => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentmonitor-schema-migrations-'));
  process.env.AGENTMONITOR_DB_PATH = path.join(tempDir, 'legacy.db');

  const schema = await import('../src/db/schema.js');
  const dbModule = await import('../src/db/connection.js');
  initSchema = schema.initSchema;
  closeDb = dbModule.closeDb;
  getDb = dbModule.getDb;
});

after(() => {
  closeDb();
  fs.rmSync(tempDir, { recursive: true, force: true });
});

test('initSchema upgrades legacy event and provider quota tables in place', () => {
  const db = getDb();
  db.exec(`
    CREATE TABLE events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id TEXT UNIQUE,
      schema_version INTEGER NOT NULL DEFAULT 1,
      session_id TEXT NOT NULL,
      agent_type TEXT NOT NULL,
      event_type TEXT NOT NULL CHECK (event_type IN ('tool_use', 'response', 'error')),
      tool_name TEXT,
      status TEXT NOT NULL DEFAULT 'success' CHECK (status IN ('success', 'error', 'timeout')),
      tokens_in INTEGER DEFAULT 0,
      tokens_out INTEGER DEFAULT 0,
      branch TEXT,
      project TEXT,
      duration_ms INTEGER,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      metadata TEXT DEFAULT '{}'
    );

    INSERT INTO events (
      event_id, session_id, agent_type, event_type, tool_name, metadata
    ) VALUES (
      'evt-legacy', 'session-legacy', 'codex', 'tool_use', 'exec_command', 'not valid json'
    );

    CREATE TABLE provider_quotas (
      agent_type TEXT NOT NULL,
      updated_at TEXT
    );
    INSERT INTO provider_quotas (agent_type, updated_at) VALUES ('codex', '2026-03-01T00:00:00Z');
  `);

  initSchema();

  const eventColumns = new Set(
    (db.prepare('PRAGMA table_info(events)').all() as Array<{ name: string }>).map(col => col.name),
  );
  for (const column of [
    'client_timestamp',
    'payload_truncated',
    'model',
    'cost_usd',
    'cache_read_tokens',
    'cache_write_tokens',
    'source',
    'study_id',
    'study',
  ]) {
    assert.ok(eventColumns.has(column), `missing migrated events.${column}`);
  }

  const eventSql = (db.prepare(
    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'",
  ).get() as { sql: string }).sql;
  assert.equal(eventSql.includes('CHECK (event_type IN'), false);

  const legacyEvent = db.prepare('SELECT metadata, payload_truncated, cache_read_tokens, cache_write_tokens, source FROM events WHERE event_id = ?')
    .get('evt-legacy') as {
      metadata: string;
      payload_truncated: number;
      cache_read_tokens: number;
      cache_write_tokens: number;
      source: string;
    };
  assert.equal(legacyEvent.metadata, '"not valid json"');
  assert.equal(legacyEvent.payload_truncated, 0);
  assert.equal(legacyEvent.cache_read_tokens, 0);
  assert.equal(legacyEvent.cache_write_tokens, 0);
  assert.equal(legacyEvent.source, 'api');

  assert.doesNotThrow(() => {
    db.prepare(`
      INSERT INTO events (event_id, session_id, agent_type, event_type, metadata)
      VALUES (?, ?, ?, ?, ?)
    `).run('evt-new-type', 'session-new', 'codex', 'session_update', '{}');
  });

  const providerQuotaColumns = new Set(
    (db.prepare('PRAGMA table_info(provider_quotas)').all() as Array<{ name: string }>).map(col => col.name),
  );
  for (const column of [
    'provider',
    'status',
    'source',
    'account_label',
    'plan_type',
    'limit_id',
    'limit_name',
    'error_message',
    'primary_used_percent',
    'primary_window_minutes',
    'primary_resets_at',
    'secondary_used_percent',
    'secondary_window_minutes',
    'secondary_resets_at',
    'credits_has_credits',
    'credits_unlimited',
    'credits_balance',
    'raw_payload',
  ]) {
    assert.ok(providerQuotaColumns.has(column), `missing migrated provider_quotas.${column}`);
  }

  db.prepare(`
    INSERT INTO provider_quotas (provider, agent_type, status, source, raw_payload)
    VALUES (?, ?, ?, ?, ?)
  `).run('codex', 'codex', 'available', 'codex-app-server', '{"ok":true}');
  const quota = db.prepare('SELECT provider, status FROM provider_quotas WHERE provider = ?')
    .get('codex') as { provider: string; status: string };
  assert.deepEqual(quota, { provider: 'codex', status: 'available' });
});
