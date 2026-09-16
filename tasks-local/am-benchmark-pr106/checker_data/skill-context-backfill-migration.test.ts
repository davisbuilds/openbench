import assert from 'node:assert/strict';
import Database from 'better-sqlite3';
import { test } from 'node:test';
import { runDataMigrations } from '../src/db/schema.js';

test('v4 invalidates eligible watched session files exactly once', () => {
  const db = new Database(':memory:');
  db.exec(`
    CREATE TABLE watched_files (
      file_path TEXT PRIMARY KEY,
      file_hash TEXT NOT NULL
    );
    CREATE TABLE browsing_sessions (
      id TEXT PRIMARY KEY,
      agent TEXT NOT NULL,
      file_path TEXT
    );
    INSERT INTO watched_files VALUES
      ('/tmp/claude.jsonl', 'claude-hash'),
      ('/tmp/codex.jsonl', 'codex-hash'),
      ('/tmp/other.db', 'other-hash'),
      ('/tmp/orphan.jsonl', 'orphan-hash');
    INSERT INTO browsing_sessions VALUES
      ('claude', 'claude', '/tmp/claude.jsonl'),
      ('codex', 'codex', '/tmp/codex.jsonl'),
      ('other', 'antigravity', '/tmp/other.db');
    PRAGMA user_version = 3;
  `);

  runDataMigrations(db);
  assert.equal(db.pragma('user_version', { simple: true }), 5);
  assert.deepEqual(
    db.prepare('SELECT file_path, file_hash FROM watched_files ORDER BY file_path').all(),
    [
      { file_path: '/tmp/claude.jsonl', file_hash: '' },
      { file_path: '/tmp/codex.jsonl', file_hash: '' },
      { file_path: '/tmp/orphan.jsonl', file_hash: 'orphan-hash' },
      { file_path: '/tmp/other.db', file_hash: 'other-hash' },
    ],
  );

  db.prepare(`UPDATE watched_files SET file_hash = 'new-hash' WHERE file_path = '/tmp/claude.jsonl'`).run();
  runDataMigrations(db);
  assert.equal(
    (db.prepare(`SELECT file_hash FROM watched_files WHERE file_path = '/tmp/claude.jsonl'`).get() as { file_hash: string }).file_hash,
    'new-hash',
  );
  db.close();
});
