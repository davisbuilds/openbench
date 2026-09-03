import { parseIntegerOption, parseOptionSet, requireOne } from '../args.js';
import { registerCommand } from '../commands.js';
import { invalidUsage, notFound } from '../errors.js';
import { effectiveBaseUrl } from '../http.js';
import { formatLiveItems, formatLiveSessions, formatMessages, formatPins, formatSessionDetail, formatSessionRows } from '../formatters/sessions.js';
import { writeJson, writeStdout } from '../output.js';
import type { CliContext } from '../output.js';

async function initDb() {
  const { initSchema } = await import('../../db/schema.js');
  const { closeDb } = await import('../../db/connection.js');
  initSchema();
  return { closeDb };
}

function parseListFilters(args: string[]) {
  const parsed = parseOptionSet(
    args,
    new Set(['--project', '--agent', '--date-from', '--date-to', '--min-messages', '--max-messages', '--limit', '--cursor']),
    new Set(['--exclude-empty']),
  );
  return {
    parsed,
    params: {
      project: parsed.values.get('--project'),
      agent: parsed.values.get('--agent'),
      date_from: parsed.values.get('--date-from'),
      date_to: parsed.values.get('--date-to'),
      min_messages: parseIntegerOption(parsed.values.get('--min-messages'), '--min-messages'),
      max_messages: parseIntegerOption(parsed.values.get('--max-messages'), '--max-messages'),
      limit: parseIntegerOption(parsed.values.get('--limit'), '--limit'),
      cursor: parsed.values.get('--cursor'),
      exclude_empty: parsed.flags.has('--exclude-empty'),
    },
  };
}

function parseSearchFilters(args: string[]) {
  const parsed = parseOptionSet(
    args,
    new Set(['--project', '--agent', '--sort', '--limit', '--cursor']),
    new Set(),
  );
  const query = requireOne(parsed.positionals, 'amon sessions search <query> [options]');
  const sort = parsed.values.get('--sort');
  if (sort && sort !== 'recent' && sort !== 'relevance') {
    throw invalidUsage(`Invalid --sort: ${sort}`);
  }
  return {
    q: query,
    project: parsed.values.get('--project'),
    agent: parsed.values.get('--agent'),
    sort: sort as 'recent' | 'relevance' | undefined,
    limit: parseIntegerOption(parsed.values.get('--limit'), '--limit'),
    cursor: parsed.values.get('--cursor'),
  };
}

function writeFormatted(ctx: CliContext, jsonValue: unknown, humanText: string): void {
  if (ctx.global.json) {
    writeJson(ctx, jsonValue);
  } else {
    writeStdout(ctx, humanText);
  }
}

function parseKindFilter(value: string | undefined): Set<string> | undefined {
  const kinds = value?.split(',').map(kind => kind.trim()).filter(Boolean);
  return kinds && kinds.length > 0 ? new Set(kinds) : undefined;
}

function objectValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function kindFromPayload(value: unknown): string | undefined {
  const obj = objectValue(value);
  if (!obj) return undefined;
  if (typeof obj.kind === 'string') return obj.kind;

  const item = objectValue(obj.item);
  if (typeof item?.kind === 'string') return item.kind;

  return undefined;
}

function liveEventKind(value: unknown): string | undefined {
  const event = objectValue(value);
  if (!event) return undefined;
  return kindFromPayload(event.payload) ?? kindFromPayload(event);
}

function shouldWriteLiveData(data: string, kindFilter: Set<string> | undefined): boolean {
  if (!kindFilter) return true;

  try {
    const parsed = JSON.parse(data) as unknown;
    const kind = liveEventKind(parsed);
    return kind ? kindFilter.has(kind) : false;
  } catch {
    return true;
  }
}

async function streamLive(
  ctx: CliContext,
  sessionId: string | undefined,
  sinceNow: boolean,
  kindFilter?: Set<string>,
): Promise<void> {
  const base = effectiveBaseUrl(ctx.global.url);
  const url = new URL(`${base}/api/v2/live/stream`);
  if (sessionId) url.searchParams.set('session_id', sessionId);
  if (sinceNow) url.searchParams.set('since', String(Number.MAX_SAFE_INTEGER));
  const res = await fetch(url);
  if (!res.ok || !res.body) {
    const { unavailable } = await import('../errors.js');
    throw unavailable(`${url.toString()} returned ${res.status}`);
  }
  const decoder = new TextDecoder();
  let buffer = '';
  function processLine(line: string): void {
    const normalized = line.endsWith('\r') ? line.slice(0, -1) : line;
    if (!normalized.startsWith('data: ')) return;
    const data = normalized.slice('data: '.length);
    if (shouldWriteLiveData(data, kindFilter)) writeStdout(ctx, data);
  }
  for await (const chunk of res.body) {
    buffer += decoder.decode(chunk, { stream: true });
    let newlineIndex = buffer.indexOf('\n');
    while (newlineIndex !== -1) {
      processLine(buffer.slice(0, newlineIndex));
      buffer = buffer.slice(newlineIndex + 1);
      newlineIndex = buffer.indexOf('\n');
    }
  }
  buffer += decoder.decode();
  if (buffer) processLine(buffer);
}

export function registerSessionLiveCommands(): void {
  registerCommand({
    name: 'sessions list',
    group: 'Session Commands',
    summary: 'List browsable sessions',
    usage: 'sessions list [--project <name>] [--agent <type>] [--limit <n>] [--json]',
    examples: ['sessions list --exclude-empty', 'sessions list --agent codex --json'],
    async handler(ctx, args) {
      const { parsed, params } = parseListFilters(args);
      if (parsed.positionals.length > 0) throw invalidUsage('Usage: amon sessions list [options]');
      const { closeDb } = await initDb();
      try {
        const { listBrowsingSessions } = await import('../../db/v2-queries.js');
        const result = listBrowsingSessions(params);
        writeFormatted(ctx, result, formatSessionRows(result.data));
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'sessions show',
    group: 'Session Commands',
    summary: 'Show session metadata',
    usage: 'sessions show <id> [--json]',
    examples: ['sessions show abc123 --json'],
    async handler(ctx, args) {
      const id = requireOne(args, 'amon sessions show <id>');
      const { closeDb } = await initDb();
      try {
        const { getBrowsingSession } = await import('../../db/v2-queries.js');
        const session = getBrowsingSession(id);
        if (!session) throw notFound(`Session not found: ${id}`);
        writeFormatted(ctx, session, formatSessionDetail(session));
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'sessions messages',
    group: 'Session Commands',
    summary: 'Show a window of session messages',
    usage: 'sessions messages <id> [--offset <n>] [--limit <n>] [--around-ordinal <n>]',
    examples: ['sessions messages abc123 --around-ordinal 80 --limit 40'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(
        args,
        new Set(['--offset', '--limit', '--around-ordinal']),
        new Set(),
      );
      const id = requireOne(parsed.positionals, 'amon sessions messages <id> [options]');
      const params = {
        offset: parseIntegerOption(parsed.values.get('--offset'), '--offset'),
        limit: parseIntegerOption(parsed.values.get('--limit'), '--limit'),
        around_ordinal: parseIntegerOption(parsed.values.get('--around-ordinal'), '--around-ordinal'),
      };
      const { closeDb } = await initDb();
      try {
        const { getBrowsingSession, getSessionMessages } = await import('../../db/v2-queries.js');
        if (!getBrowsingSession(id)) throw notFound(`Session not found: ${id}`);
        const result = getSessionMessages(id, params);
        writeFormatted(ctx, result, formatMessages(result.data));
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'sessions search',
    group: 'Session Commands',
    summary: 'Search transcript messages',
    usage: 'sessions search <query> [--sort recent|relevance] [--limit <n>]',
    examples: ['sessions search "rate limit" --sort relevance --json'],
    async handler(ctx, args) {
      const params = parseSearchFilters(args);
      const { closeDb } = await initDb();
      try {
        const { searchMessages } = await import('../../db/v2-queries.js');
        const result = searchMessages(params);
        const rows = result.data.map(row => ({
          id: row.session_id,
          project: row.session_project,
          agent: row.session_agent,
          first_message: row.snippet,
          started_at: row.session_started_at,
          ended_at: row.session_ended_at,
          message_count: row.message_ordinal,
          user_message_count: 0,
          parent_session_id: null,
          relationship_type: null,
          live_status: null,
          last_item_at: null,
          integration_mode: null,
          fidelity: null,
          capabilities: null,
          file_path: null,
          file_size: null,
          file_hash: null,
          context_used_tokens: null,
          context_window_tokens: null,
          context_pct: null,
        }));
        writeFormatted(ctx, result, formatSessionRows(rows));
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'pins list',
    group: 'Session Commands',
    summary: 'List pinned transcript moments',
    usage: 'pins list [--project <name>] [--json]',
    examples: ['pins list --project agentmonitor --json'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(args, new Set(['--project']), new Set());
      if (parsed.positionals.length > 0) throw invalidUsage('Usage: amon pins list [options]');
      const { closeDb } = await initDb();
      try {
        const { listPinnedMessages } = await import('../../db/v2-queries.js');
        const pins = listPinnedMessages({ project: parsed.values.get('--project') });
        writeFormatted(ctx, { data: pins }, formatPins(pins));
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'live sessions',
    group: 'Live Commands',
    summary: 'List live-projected sessions',
    usage: 'live sessions [--active-only] [--agent <type>] [--fidelity <level>]',
    examples: ['live sessions --active-only --json'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(
        args,
        new Set(['--project', '--agent', '--status', '--fidelity', '--limit', '--cursor']),
        new Set(['--active-only']),
      );
      if (parsed.positionals.length > 0) throw invalidUsage('Usage: amon live sessions [options]');
      const { closeDb } = await initDb();
      try {
        const { listLiveSessions } = await import('../../db/v2-queries.js');
        const result = listLiveSessions({
          project: parsed.values.get('--project'),
          agent: parsed.values.get('--agent'),
          live_status: parsed.values.get('--status'),
          fidelity: parsed.values.get('--fidelity'),
          limit: parseIntegerOption(parsed.values.get('--limit'), '--limit'),
          cursor: parsed.values.get('--cursor'),
          active_only: parsed.flags.has('--active-only'),
        });
        writeFormatted(ctx, result, formatLiveSessions(result.data));
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'live items',
    group: 'Live Commands',
    summary: 'List live items for a session',
    usage: 'live items <id> [--limit <n>] [--cursor <id>] [--kinds <csv>]',
    examples: ['live items abc123 --kinds message,tool_call --json'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(args, new Set(['--limit', '--cursor', '--kinds']), new Set());
      const id = requireOne(parsed.positionals, 'amon live items <id> [options]');
      const { closeDb } = await initDb();
      try {
        const { getLiveSession, getSessionItems } = await import('../../db/v2-queries.js');
        if (!getLiveSession(id)) throw notFound(`Live session not found: ${id}`);
        const result = getSessionItems(id, {
          limit: parseIntegerOption(parsed.values.get('--limit'), '--limit'),
          cursor: parsed.values.get('--cursor'),
          kinds: parsed.values.get('--kinds')?.split(',').map(value => value.trim()).filter(Boolean),
        });
        writeFormatted(ctx, result, formatLiveItems(result.data));
      } finally {
        closeDb();
      }
    },
  });

  registerCommand({
    name: 'live watch',
    group: 'Live Commands',
    summary: 'Stream live SSE events as NDJSON',
    usage: 'live watch [id] [--since-now] [--kinds <csv>] [--url <url>]',
    examples: ['live watch', 'live watch abc123 --since-now', 'live watch --kinds user_message,tool_call'],
    async handler(ctx, args) {
      const parsed = parseOptionSet(args, new Set(['--kinds']), new Set(['--since-now']));
      const sessionId = parsed.positionals.length > 0 ? requireOne(parsed.positionals, 'amon live watch [id] [options]') : undefined;
      await streamLive(ctx, sessionId, parsed.flags.has('--since-now'), parseKindFilter(parsed.values.get('--kinds')));
    },
  });
}
