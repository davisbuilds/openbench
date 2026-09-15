import fs from 'fs';
import { getDb } from '../db/connection.js';
import { insertEvent, refreshImportedCodexEventModel, setSessionMode } from '../db/queries.js';
import { discoverClaudeCodeLogs, parseClaudeCodeFile, hashFile as hashClaudeFile } from './claude-code.js';
import { discoverCodexLogs, parseCodexFile, hashFile as hashCodexFile } from './codex.js';
import { discoverAntigravityLogs, parseAntigravityFile, hashFile as hashAntigravityFile } from './antigravity.js';
import type { NormalizedIngestEvent } from '../contracts/event-contract.js';
import { createConfig } from '../config.js';
import { safelyMaintainTraceSummaryForEvent, safelyMaintainTraceSummaryForSession } from '../trace-quality/service.js';

// ─── Types ──────────────────────────────────────────────────────────────

export type ImportSource = 'claude-code' | 'codex' | 'antigravity' | 'all';

export interface ImportOptions {
  source: ImportSource;
  from?: Date;
  to?: Date;
  dryRun?: boolean;
  force?: boolean;
  claudeDir?: string;
  codexDir?: string;
  antigravityDir?: string;
  excludePatterns?: string[];
}

export interface ImportFileResult {
  path: string;
  source: string;
  eventsFound: number;
  eventsImported: number;
  eventsRefreshed: number;
  skippedDuplicate: number;
  skippedUnchanged: boolean;
}

export interface ImportResult {
  files: ImportFileResult[];
  totalFiles: number;
  totalEventsFound: number;
  totalEventsImported: number;
  totalEventsRefreshed: number;
  totalDuplicates: number;
  skippedFiles: number;
}

// ─── Import state DB helpers ────────────────────────────────────────────

interface ImportStateRow {
  file_path: string;
  file_hash: string;
  file_size: number;
  source: string;
  events_imported: number;
  imported_at: string;
}

function getImportState(filePath: string): ImportStateRow | undefined {
  const db = getDb();
  return db.prepare('SELECT * FROM import_state WHERE file_path = ?').get(filePath) as ImportStateRow | undefined;
}

function setImportState(filePath: string, hash: string, size: number, source: string, eventsImported: number): void {
  const db = getDb();
  db.prepare(`
    INSERT INTO import_state (file_path, file_hash, file_size, source, events_imported, imported_at)
    VALUES (?, ?, ?, ?, ?, datetime('now'))
    ON CONFLICT(file_path) DO UPDATE SET
      file_hash = excluded.file_hash,
      file_size = excluded.file_size,
      events_imported = excluded.events_imported,
      imported_at = datetime('now')
  `).run(filePath, hash, size, source, eventsImported);
}

// ─── Core import logic ──────────────────────────────────────────────────

function importEvents(
  events: NormalizedIngestEvent[],
  dryRun: boolean,
): { imported: number; refreshed: number; duplicates: number } {
  let imported = 0;
  let refreshed = 0;
  let duplicates = 0;

  if (dryRun) {
    return { imported: events.length, refreshed: 0, duplicates: 0 };
  }

  // Invocation mode is a session-level constant carried on events. Collect it
  // here and apply once per session below, so it backfills even when every event
  // is a duplicate (upsertSession inside insertEvent is skipped on the dup path).
  const sessionModes = new Map<string, 'interactive' | 'headless'>();
  const refreshedSessions = new Set<string>();

  for (const event of events) {
    if (event.mode) sessionModes.set(event.session_id, event.mode);
    const row = insertEvent(event);
    if (row) {
      imported++;
      safelyMaintainTraceSummaryForEvent(row.id, 'historical import');
    } else {
      duplicates++;
      const refreshedEvent = refreshImportedCodexEventModel(event);
      if (refreshedEvent) {
        refreshed++;
        refreshedSessions.add(refreshedEvent.sessionId);
      }
    }
  }

  for (const [sessionId, mode] of sessionModes) {
    setSessionMode(sessionId, mode);
  }
  for (const sessionId of refreshedSessions) {
    safelyMaintainTraceSummaryForSession(sessionId, 'Codex model-attribution refresh');
  }

  return { imported, refreshed, duplicates };
}

function processFile(
  filePath: string,
  source: 'claude-code' | 'codex' | 'antigravity',
  options: ImportOptions,
): ImportFileResult {
  const stat = fs.statSync(filePath);
  const hashFn =
    source === 'claude-code' ? hashClaudeFile : source === 'codex' ? hashCodexFile : hashAntigravityFile;
  const currentHash = hashFn(filePath);

  // Check import state (skip if unchanged, unless --force)
  if (!options.force) {
    const state = getImportState(filePath);
    if (state && state.file_hash === currentHash) {
      return {
        path: filePath,
        source,
        eventsFound: 0,
        eventsImported: 0,
        eventsRefreshed: 0,
        skippedDuplicate: 0,
        skippedUnchanged: true,
      };
    }
  }

  // Parse the file (each source has its own option needs)
  const events =
    source === 'claude-code'
      ? parseClaudeCodeFile(filePath, { from: options.from, to: options.to })
      : source === 'codex'
        ? parseCodexFile(filePath, { from: options.from, to: options.to, codexDir: options.codexDir })
        : parseAntigravityFile(filePath, { from: options.from, to: options.to });

  // Import events
  const { imported, refreshed, duplicates } = importEvents(events, options.dryRun ?? false);

  // Record import state (unless dry run or date-scoped import).
  // Date-scoped imports are partial — caching the hash would cause a later
  // full import to skip the file, permanently losing the excluded events.
  const isDateScoped = options.from !== undefined || options.to !== undefined;
  if (!options.dryRun && !isDateScoped) {
    setImportState(filePath, currentHash, stat.size, source, imported);
  }

  return {
    path: filePath,
    source,
    eventsFound: events.length,
    eventsImported: imported,
    eventsRefreshed: refreshed,
    skippedDuplicate: duplicates,
    skippedUnchanged: false,
  };
}

// ─── Public API ─────────────────────────────────────────────────────────

export function runImport(options: ImportOptions): ImportResult {
  const files: ImportFileResult[] = [];
  const runtimeConfig = createConfig();
  const excludePatterns = options.excludePatterns ?? runtimeConfig.sync.excludePatterns;

  // Discover files
  const claudeFiles = (options.source === 'claude-code' || options.source === 'all')
    ? discoverClaudeCodeLogs(options.claudeDir ?? runtimeConfig.claudeDir, { excludePatterns })
    : [];
  const codexFiles = (options.source === 'codex' || options.source === 'all')
    ? discoverCodexLogs(options.codexDir, { excludePatterns })
    : [];
  const antigravityFiles = (options.source === 'antigravity' || options.source === 'all')
    ? discoverAntigravityLogs(options.antigravityDir, { excludePatterns })
    : [];

  // Process Claude Code files
  for (const filePath of claudeFiles) {
    files.push(processFile(filePath, 'claude-code', options));
  }

  // Process Codex files
  for (const filePath of codexFiles) {
    files.push(processFile(filePath, 'codex', options));
  }

  // Process Antigravity files
  for (const filePath of antigravityFiles) {
    files.push(processFile(filePath, 'antigravity', options));
  }

  // Aggregate results
  let totalEventsFound = 0;
  let totalEventsImported = 0;
  let totalEventsRefreshed = 0;
  let totalDuplicates = 0;
  let skippedFiles = 0;

  for (const f of files) {
    totalEventsFound += f.eventsFound;
    totalEventsImported += f.eventsImported;
    totalEventsRefreshed += f.eventsRefreshed;
    totalDuplicates += f.skippedDuplicate;
    if (f.skippedUnchanged) skippedFiles++;
  }

  return {
    files,
    totalFiles: files.length,
    totalEventsFound,
    totalEventsImported,
    totalEventsRefreshed,
    totalDuplicates,
    skippedFiles,
  };
}
