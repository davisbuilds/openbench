import os from 'node:os';
import path from 'node:path';
import { parseDateOption, parseOptionSet, rejectExtraPositionals } from '../args.js';
import { registerCommand } from '../commands.js';
import { invalidUsage, partialSuccess } from '../errors.js';
import { writeJson, writeStdout } from '../output.js';
import type { CliContext } from '../output.js';
import type Database from 'better-sqlite3';

type ImportSource = 'claude-code' | 'codex' | 'antigravity' | 'all';
type SyncSource = 'claude' | 'codex' | 'all';

function parseImportSource(value: string | undefined): ImportSource {
  const source = value ?? 'all';
  if (source !== 'claude-code' && source !== 'codex' && source !== 'antigravity' && source !== 'all') {
    throw invalidUsage(`Invalid --source: ${source}. Expected claude-code, codex, antigravity, or all.`);
  }
  return source;
}

function parseSyncSource(value: string | undefined): SyncSource {
  const source = value ?? 'all';
  if (source !== 'claude' && source !== 'codex' && source !== 'all') {
    throw invalidUsage(`Invalid --source: ${source}. Expected claude, codex, or all.`);
  }
  return source;
}

function printSummary(ctx: CliContext, title: string, rows: Record<string, unknown>): void {
  if (ctx.global.json) {
    writeJson(ctx, rows);
    return;
  }
  const lines = [title, ...Object.entries(rows).map(([key, value]) => `  ${key}: ${String(value)}`)];
  writeStdout(ctx, lines.join('\n'));
}

// eslint-disable-next-line no-control-regex
const ANSI_ESCAPE_RE = /\u001b\[[0-9;]*m/g;

function cleanText(text: string): string {
  return text.replace(ANSI_ESCAPE_RE, '').replace(/\s+/g, ' ').trim();
}

function derivePreviewFromText(text: string | null | undefined): string | null {
  if (!text) return null;
  if (text.includes('<local-command-caveat>')) return null;
  if (text.includes('<command-name>')) return null;
  if (text.includes('<local-command-stdout>')) return null;
  if (text.includes('<local-command-stderr>')) return null;
  return cleanText(text).slice(0, 200) || null;
}

function backfillStaleSessionTitles(db: Database.Database): { updated: number; fallbackOnly: number } {
  const sessions = db.prepare(`
    SELECT id
    FROM browsing_sessions
    WHERE first_message LIKE '%<local-command-caveat>%'
       OR first_message LIKE '%<command-name>%'
       OR first_message LIKE '%<local-command-stdout>%'
       OR first_message LIKE '%<local-command-stderr>%'
  `).all() as Array<{ id: string }>;

  const listMessages = db.prepare(`
    SELECT content
    FROM messages
    WHERE session_id = ?
    ORDER BY ordinal
  `);
  const updateSession = db.prepare('UPDATE browsing_sessions SET first_message = ? WHERE id = ?');

  let updated = 0;
  let fallbackOnly = 0;

  const txn = db.transaction(() => {
    for (const session of sessions) {
      const messages = listMessages.all(session.id) as Array<{ content: string }>;
      let preview: string | null = null;

      for (const message of messages) {
        try {
          const blocks = JSON.parse(message.content) as Array<{ type?: string; text?: string }>;
          const textBlock = blocks.find(block => block?.type === 'text' && typeof block.text === 'string' && block.text.trim());
          preview = derivePreviewFromText(textBlock?.text);
        } catch {
          preview = derivePreviewFromText(message.content);
        }

        if (preview) break;
      }

      if (!preview) {
        preview = 'Local command activity';
        fallbackOnly++;
      }

      updateSession.run(preview, session.id);
      updated++;
    }
  });

  txn();
  return { updated, fallbackOnly };
}

export function registerMaintenanceCommands(): void {
  registerCommand({
    name: 'import',
    group: 'Data Commands',
    summary: 'Import historical Claude Code and Codex events',
    usage: 'import [--source claude-code|codex|antigravity|all] [--from <date>] [--to <date>] [--dry-run] [--force]',
    examples: ['import --source codex --dry-run', 'import --source all --from 2026-06-01 --json'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(
        args,
        new Set(['--source', '--from', '--to', '--claude-dir', '--codex-dir', '--antigravity-dir']),
        new Set(['--dry-run', '--force']),
      );
      rejectExtraPositionals(parsed.positionals, 'amon import [options]');
      const source = parseImportSource(parsed.values.get('--source'));
      const from = parseDateOption(parsed.values.get('--from'), '--from');
      const to = parseDateOption(parsed.values.get('--to'), '--to');

      const { initSchema } = await import('../../db/schema.js');
      const { closeDb } = await import('../../db/connection.js');
      const { runImport } = await import('../../import/index.js');
      initSchema();
      try {
        const result = runImport({
          source,
          from: from ? new Date(from) : undefined,
          to: to ? new Date(to) : undefined,
          dryRun: parsed.flags.has('--dry-run'),
          force: parsed.flags.has('--force'),
          claudeDir: parsed.values.get('--claude-dir'),
          codexDir: parsed.values.get('--codex-dir'),
          antigravityDir: parsed.values.get('--antigravity-dir'),
        });
        const payload = {
          dry_run: parsed.flags.has('--dry-run'),
          source,
          total_files: result.totalFiles,
          skipped_files: result.skippedFiles,
          events_found: result.totalEventsFound,
          events_imported: result.totalEventsImported,
          events_refreshed: result.totalEventsRefreshed,
          duplicates: result.totalDuplicates,
          files: result.files,
        };
        printSummary(ctx, 'Import results', payload);
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'sync sessions',
    group: 'Data Commands',
    summary: 'Sync Claude and Codex session-browser files',
    usage: 'sync sessions [--source claude|codex|all] [--dry-run] [--force]',
    examples: ['sync sessions --source all --dry-run', 'sync sessions --source codex --force --json'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(
        args,
        new Set(['--source', '--claude-dir', '--codex-home']),
        new Set(['--dry-run', '--force']),
      );
      rejectExtraPositionals(parsed.positionals, 'amon sync sessions [options]');
      const source = parseSyncSource(parsed.values.get('--source'));
      const dryRun = parsed.flags.has('--dry-run');
      const { createConfig } = await import('../../config.js');
      const runtimeConfig = createConfig();
      const claudeDir = parsed.values.get('--claude-dir') ?? runtimeConfig.claudeDir;
      const codexHome = parsed.values.get('--codex-home') ?? process.env.CODEX_HOME ?? path.join(os.homedir(), '.codex');
      const { discoverSessionFiles, discoverCodexSessionFiles, syncAllFiles, syncAllCodexFiles } = await import('../../watcher/index.js');
      const excludePatterns = runtimeConfig.sync.excludePatterns;

      if (dryRun) {
        const claudeFiles = source === 'claude' || source === 'all'
          ? discoverSessionFiles(claudeDir, { excludePatterns })
          : [];
        const codexFiles = source === 'codex' || source === 'all'
          ? discoverCodexSessionFiles(codexHome, { excludePatterns })
          : [];
        printSummary(ctx, 'Session sync preview', {
          dry_run: true,
          source,
          claude_files: claudeFiles.length,
          codex_files: codexFiles.length,
          total_files: claudeFiles.length + codexFiles.length,
        });
        return;
      }

      const { initSchema } = await import('../../db/schema.js');
      const { closeDb, getDb } = await import('../../db/connection.js');
      initSchema();
      try {
        const db = getDb();
        const claude = source === 'claude' || source === 'all'
          ? syncAllFiles(db, claudeDir, { force: parsed.flags.has('--force'), excludePatterns })
          : { parsed: 0, skipped: 0, errors: 0, total: 0 };
        const staleTitleBackfill = source === 'claude' || source === 'all'
          ? backfillStaleSessionTitles(db)
          : { updated: 0, fallbackOnly: 0 };
        const codex = source === 'codex' || source === 'all'
          ? syncAllCodexFiles(db, codexHome, { force: parsed.flags.has('--force'), excludePatterns })
          : { parsed: 0, skipped: 0, errors: 0, total: 0 };
        const payload = {
          dry_run: false,
          source,
          claude,
          codex,
          total_files: claude.total + codex.total,
          parsed: claude.parsed + codex.parsed,
          skipped: claude.skipped + codex.skipped,
          errors: claude.errors + codex.errors,
          stale_title_backfill: staleTitleBackfill,
        };
        printSummary(ctx, 'Session sync results', payload);
        if (payload.errors > 0) throw partialSuccess(`Session sync completed with ${payload.errors} error(s).`);
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'costs recalc',
    group: 'Data Commands',
    summary: 'Recalculate event costs from pricing metadata',
    usage: 'costs recalc [--dry-run]',
    examples: ['costs recalc --dry-run --json'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(args, new Set(), new Set(['--dry-run']));
      rejectExtraPositionals(parsed.positionals, 'amon costs recalc [--dry-run]');
      const { initSchema } = await import('../../db/schema.js');
      const { closeDb, getDb } = await import('../../db/connection.js');
      const { pricingRegistry } = await import('../../pricing/index.js');
      initSchema();
      try {
        const db = getDb();
        const events = db.prepare(`
          SELECT id, model, tokens_in, tokens_out, cache_read_tokens, cache_write_tokens, cost_usd
          FROM events
          WHERE model IS NOT NULL
            AND (tokens_in > 0 OR tokens_out > 0 OR cache_read_tokens > 0 OR cache_write_tokens > 0)
        `).all() as Array<{
          id: number;
          model: string;
          tokens_in: number;
          tokens_out: number;
          cache_read_tokens: number;
          cache_write_tokens: number;
          cost_usd: number | null;
        }>;
        const update = db.prepare('UPDATE events SET cost_usd = ? WHERE id = ?');
        let updated = 0;
        let unchanged = 0;
        let unknownModel = 0;
        const run = db.transaction(() => {
          for (const event of events) {
            const cost = pricingRegistry.calculate(event.model, {
              input: event.tokens_in,
              output: event.tokens_out,
              cacheRead: event.cache_read_tokens,
              cacheWrite: event.cache_write_tokens,
            });
            if (cost === null) {
              unknownModel++;
              continue;
            }
            const rounded = Math.round(cost * 1e10) / 1e10;
            const existing = event.cost_usd !== null ? Math.round(event.cost_usd * 1e10) / 1e10 : null;
            if (existing === rounded) {
              unchanged++;
              continue;
            }
            if (!parsed.flags.has('--dry-run')) update.run(rounded, event.id);
            updated++;
          }
        });
        run();
        printSummary(ctx, 'Cost recalculation results', {
          dry_run: parsed.flags.has('--dry-run'),
          scanned: events.length,
          updated,
          unchanged,
          unknown_model: unknownModel,
        });
      } finally {
        closeDb();
      }
    },
  });

}
