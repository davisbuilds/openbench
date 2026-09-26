import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test, { after, before } from 'node:test';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'agentmonitor-codex-model-migration-'));
process.env.AGENTMONITOR_DB_PATH = path.join(tempDir, 'agentmonitor.db');

const { initSchema, runDataMigrations } = await import('../src/db/schema.js');
const { closeDb, getDb } = await import('../src/db/connection.js');

before(() => {
  initSchema();
});

after(() => {
  closeDb();
  fs.rmSync(tempDir, { recursive: true, force: true });
});

test('the GPT-5.6 model-attribution migration invalidates only Codex event imports once', () => {
  const db = getDb();
  db.prepare(`
    INSERT INTO import_state (file_path, file_hash, file_size, source, events_imported)
    VALUES (?, ?, 1, ?, 1)
  `).run('/fake/codex.jsonl', 'codex-hash', 'codex');
  db.prepare(`
    INSERT INTO import_state (file_path, file_hash, file_size, source, events_imported)
    VALUES (?, ?, 1, ?, 1)
  `).run('/fake/claude.jsonl', 'claude-hash', 'claude-code');

  db.pragma('user_version = 2');
  runDataMigrations(db);

  const rows = db.prepare('SELECT source, file_hash FROM import_state ORDER BY source').all() as Array<{
    source: string;
    file_hash: string;
  }>;
  assert.deepEqual(rows, [
    { source: 'claude-code', file_hash: 'claude-hash' },
    { source: 'codex', file_hash: '' },
  ]);
  assert.equal(db.pragma('user_version', { simple: true }), 5);

  db.prepare("UPDATE import_state SET file_hash = 'restored' WHERE source = 'codex'").run();
  runDataMigrations(db);
  assert.equal(
    (db.prepare("SELECT file_hash FROM import_state WHERE source = 'codex'").get() as { file_hash: string }).file_hash,
    'restored',
  );
});
