import fs from 'fs';
import path from 'path';
import os from 'os';
import crypto from 'crypto';
import type Database from 'better-sqlite3';
import { parseSessionMessages, insertParsedSession } from '../parser/claude-code.js';
import { parseCodexSessionMessages } from '../parser/codex-sessions.js';
import { parseAntigravitySessions } from '../parser/antigravity-sessions.js';
import { syncClaudeLiveSession, type ClaudeLiveSyncResult } from '../live/claude-adapter.js';
import { syncCodexLiveSession } from '../live/codex-adapter.js';
import { syncAntigravityLiveSession } from '../live/antigravity-adapter.js';
import { discoverAntigravityLogs } from '../import/antigravity.js';
import { discoverJsonlFilesRecursive } from '../util/file-discovery.js';
import { safelyMaintainTraceSummaryForSession } from '../trace-quality/service.js';
import { setSessionMode } from '../db/queries.js';

// --- File hashing ---

function hashFile(filePath: string): string {
  const content = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(content).digest('hex');
}

// --- Discover session files ---

export function discoverSessionFiles(claudeDir: string, options: SyncOptions = {}): string[] {
  const projectsDir = path.join(claudeDir, 'projects');
  return discoverJsonlFilesRecursive(projectsDir, { excludePatterns: options.excludePatterns });
}

// --- Sync a single session file ---

export type SyncResult = 'parsed' | 'skipped' | 'error';

interface SyncOptions {
  force?: boolean;
  excludePatterns?: string[];
}

export interface SyncSessionOutcome {
  result: SyncResult;
  live?: ClaudeLiveSyncResult;
  session_id?: string;
}

interface WatchedFileState {
  file_hash: string;
  status: SyncResult;
}

interface MissingProjectionRow {
  file_path: string;
}

function getWatchedFileState(db: Database.Database, filePath: string): WatchedFileState | undefined {
  return db.prepare(
    'SELECT file_hash, status FROM watched_files WHERE file_path = ?'
  ).get(filePath) as WatchedFileState | undefined;
}

function upsertWatchedFile(
  db: Database.Database,
  filePath: string,
  fileHash: string,
  fileMtime: string,
  status: SyncResult,
): void {
  db.prepare(`
    INSERT INTO watched_files (file_path, file_hash, file_mtime, status, last_parsed_at)
    VALUES (?, ?, ?, ?, datetime('now'))
    ON CONFLICT(file_path) DO UPDATE SET
      file_hash = excluded.file_hash,
      file_mtime = excluded.file_mtime,
      status = excluded.status,
      last_parsed_at = datetime('now')
  `).run(filePath, fileHash, fileMtime, status);
}

/**
 * Return currently discoverable transcript files whose cache says parsing
 * succeeded but whose browser projection is absent. Stale cache rows for files
 * no longer on disk and intentionally skipped files are excluded by design.
 */
export function findMissingSessionProjections(
  db: Database.Database,
  currentFilePaths: Iterable<string>,
): string[] {
  const currentFiles = new Set(currentFilePaths);
  if (currentFiles.size === 0) return [];

  const missingRows = db.prepare(`
    SELECT wf.file_path
    FROM watched_files wf
    WHERE wf.status = 'parsed'
      AND NOT EXISTS (
        SELECT 1 FROM browsing_sessions bs WHERE bs.file_path = wf.file_path
      )
  `).all() as MissingProjectionRow[];

  return missingRows
    .map(row => row.file_path)
    .filter(filePath => currentFiles.has(filePath))
    .sort();
}

export function syncSessionFileDetailed(
  db: Database.Database,
  filePath: string,
  options: SyncOptions = {},
): SyncSessionOutcome {
  let fileHash = 'error';
  let fileMtime = '';
  try {
    const stat = fs.statSync(filePath);
    fileHash = hashFile(filePath);
    fileMtime = stat.mtime.toISOString();

    // Check watched_files for existing record
    const existing = getWatchedFileState(db, filePath);
    if (!options.force && existing?.file_hash === fileHash && existing.status !== 'error') {
      return { result: 'skipped' };
    }

    // Read and parse the file
    const content = fs.readFileSync(filePath, 'utf-8');
    const sessionId = path.basename(filePath, '.jsonl');
    const parsed = parseSessionMessages(content, sessionId, filePath);

    // Skip files with no messages (non-interactive sessions)
    if (parsed.messages.length === 0) {
      upsertWatchedFile(db, filePath, fileHash, fileMtime, 'skipped');
      return { result: 'skipped', session_id: sessionId };
    }

    // Insert parsed data
    insertParsedSession(db, parsed, filePath, stat.size, fileHash);
    const live = syncClaudeLiveSession(db, parsed);
    // Stamp the Monitor session's invocation mode from the JSONL the watcher
    // already parsed, so a live session gets its headless/interactive pill
    // without waiting for the next auto-import tick. No-op if the Monitor
    // session row does not exist yet (created by hooks/import, not the watcher).
    if (parsed.metadata.mode) setSessionMode(parsed.metadata.session_id, parsed.metadata.mode);
    safelyMaintainTraceSummaryForSession(sessionId, 'claude session sync');

    // Update watched_files
    upsertWatchedFile(db, filePath, fileHash, fileMtime, 'parsed');

    return { result: 'parsed', live, session_id: sessionId };
  } catch (err) {
    console.error(`[watcher] Failed to sync ${filePath}:`, err);
    try {
      upsertWatchedFile(db, filePath, fileHash, fileMtime, 'error');
    } catch (dbErr) {
      console.error(`[watcher] Failed to record error state for ${filePath}:`, dbErr);
    }
    return { result: 'error' };
  }
}

export function syncSessionFile(db: Database.Database, filePath: string, options: SyncOptions = {}): SyncResult {
  return syncSessionFileDetailed(db, filePath, options).result;
}

// --- Sync all discovered files ---

export interface SyncStats {
  parsed: number;
  skipped: number;
  errors: number;
  total: number;
}

export function syncAllFiles(db: Database.Database, claudeDir: string, options: SyncOptions = {}): SyncStats {
  const files = discoverSessionFiles(claudeDir, options);
  const stats: SyncStats = { parsed: 0, skipped: 0, errors: 0, total: files.length };

  for (const filePath of files) {
    const result = syncSessionFile(db, filePath, options);
    stats[result === 'parsed' ? 'parsed' : result === 'skipped' ? 'skipped' : 'errors']++;
  }

  return stats;
}

// --- Codex session file support ---

export function discoverCodexSessionFiles(codexHome?: string, options: SyncOptions = {}): string[] {
  const base = codexHome ?? process.env.CODEX_HOME ?? path.join(os.homedir(), '.codex');
  const sessionsDir = path.join(base, 'sessions');
  return discoverJsonlFilesRecursive(sessionsDir, { excludePatterns: options.excludePatterns });
}

export function syncCodexSessionFileDetailed(
  db: Database.Database,
  filePath: string,
  options: SyncOptions = {},
): SyncSessionOutcome {
  let fileHash = 'error';
  let fileMtime = '';
  try {
    const stat = fs.statSync(filePath);
    fileHash = hashFile(filePath);
    fileMtime = stat.mtime.toISOString();

    const existing = getWatchedFileState(db, filePath);
    if (!options.force && existing?.file_hash === fileHash && existing.status !== 'error') {
      return { result: 'skipped' };
    }

    const content = fs.readFileSync(filePath, 'utf-8');
    const sessionId = path.basename(filePath, '.jsonl');
    const parsed = parseCodexSessionMessages(content, sessionId, filePath);

    if (parsed.messages.length === 0) {
      upsertWatchedFile(db, filePath, fileHash, fileMtime, 'skipped');
      return { result: 'skipped', session_id: sessionId };
    }

    insertParsedSession(db, parsed, filePath, stat.size, fileHash);
    const live = syncCodexLiveSession(db, parsed);
    // The Monitor session row is keyed by the Codex session UUID (session_meta.id),
    // not the rollout filename the watcher uses for browsing_sessions — match the
    // import's id resolution so mode lands on the right row. No-op if absent.
    if (parsed.metadata.mode) {
      const uuid = sessionId.match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
      setSessionMode(uuid?.[1] ?? sessionId, parsed.metadata.mode);
    }
    safelyMaintainTraceSummaryForSession(sessionId, 'codex session sync');

    upsertWatchedFile(db, filePath, fileHash, fileMtime, 'parsed');

    return { result: 'parsed', live, session_id: sessionId };
  } catch (err) {
    console.error(`[watcher] Failed to sync Codex ${filePath}:`, err);
    try {
      upsertWatchedFile(db, filePath, fileHash, fileMtime, 'error');
    } catch (dbErr) {
      console.error(`[watcher] Failed to record error state for ${filePath}:`, dbErr);
    }
    return { result: 'error' };
  }
}

export function syncCodexSessionFile(db: Database.Database, filePath: string, options: SyncOptions = {}): SyncResult {
  return syncCodexSessionFileDetailed(db, filePath, options).result;
}

export function syncAllCodexFiles(db: Database.Database, codexHome?: string, options: SyncOptions = {}): SyncStats {
  const files = discoverCodexSessionFiles(codexHome, options);
  const stats: SyncStats = { parsed: 0, skipped: 0, errors: 0, total: files.length };

  for (const filePath of files) {
    const result = syncCodexSessionFileDetailed(db, filePath, options).result;
    stats[result === 'parsed' ? 'parsed' : result === 'skipped' ? 'skipped' : 'errors']++;
  }

  return stats;
}

// --- Antigravity conversation DB support ---

/**
 * Change token for an Antigravity conversation DB. Folds the SQLite sidecars
 * (`-wal`, `-shm`) into the hash: when a conversation DB is still open in WAL
 * mode, newly committed steps can live only in `<uuid>.db-wal` while the main
 * `.db` is byte-unchanged. Hashing the main file alone would make the periodic
 * resync see the same hash and skip, leaving the browser/search/trace-quality
 * projection stale until SQLite checkpoints. Folding in the sidecars means any
 * committed WAL frame flips the token (over-parsing on a spurious change is
 * harmless — `insertParsedSession` is delete-and-reinsert). (Codex review, PR #57.)
 */
export function hashAntigravityDb(filePath: string): string {
  const h = crypto.createHash('sha256');
  h.update(fs.readFileSync(filePath));
  for (const suffix of ['-wal', '-shm']) {
    try {
      h.update(suffix);
      h.update(fs.readFileSync(filePath + suffix));
    } catch {
      // sidecar absent (checkpointed / not in WAL mode) — nothing to fold in
    }
  }
  return h.digest('hex');
}

function syncAntigravitySessionFileDetailed(
  db: Database.Database,
  filePath: string,
  options: SyncOptions = {},
): SyncSessionOutcome {
  let fileHash = 'error';
  let fileMtime = '';
  try {
    const stat = fs.statSync(filePath);
    fileHash = hashAntigravityDb(filePath);
    fileMtime = stat.mtime.toISOString();

    const existing = getWatchedFileState(db, filePath);
    if (!options.force && existing?.file_hash === fileHash && existing.status !== 'error') {
      return { result: 'skipped' };
    }

    const sessionId = path.basename(filePath, '.db');
    const parsed = parseAntigravitySessions(filePath);

    if (parsed.messages.length === 0) {
      upsertWatchedFile(db, filePath, fileHash, fileMtime, 'skipped');
      return { result: 'skipped', session_id: sessionId };
    }

    insertParsedSession(db, parsed, filePath, stat.size, fileHash);
    const live = syncAntigravityLiveSession(db, parsed);
    safelyMaintainTraceSummaryForSession(sessionId, 'antigravity session sync');

    upsertWatchedFile(db, filePath, fileHash, fileMtime, 'parsed');

    return { result: 'parsed', live, session_id: sessionId };
  } catch (err) {
    console.error(`[watcher] Failed to sync Antigravity ${filePath}:`, err);
    try {
      upsertWatchedFile(db, filePath, fileHash, fileMtime, 'error');
    } catch (dbErr) {
      console.error(`[watcher] Failed to record error state for ${filePath}:`, dbErr);
    }
    return { result: 'error' };
  }
}

export function syncAllAntigravityFiles(db: Database.Database, dir?: string, options: SyncOptions = {}): SyncStats {
  const files = discoverAntigravityLogs(dir, { excludePatterns: options.excludePatterns });
  const stats: SyncStats = { parsed: 0, skipped: 0, errors: 0, total: files.length };

  for (const filePath of files) {
    const result = syncAntigravitySessionFileDetailed(db, filePath, options).result;
    stats[result === 'parsed' ? 'parsed' : result === 'skipped' ? 'skipped' : 'errors']++;
  }

  return stats;
}
