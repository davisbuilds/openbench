import { getDb } from './connection.js';
import { config } from '../config.js';
import { syncCodexSummaryLiveEvent } from '../live/codex-adapter.js';
import { pricingRegistry } from '../pricing/index.js';
import { resolveGitBranch } from '../util/git-branch.js';
import type {
  EventStatus,
  EventType,
  EventSource,
  NormalizedIngestEvent,
} from '../contracts/event-contract.js';
import { excludeOverlappingCodexOtelUsageCondition, reconciledUsageSum } from './usage-reconciliation.js';

// --- Agents ---

function upsertAgent(id: string, agentType: string, name?: string): void {
  const db = getDb();
  db.prepare(`
    INSERT INTO agents (id, agent_type, name)
    VALUES (?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET last_seen_at = datetime('now')
  `).run(id, agentType, name || null);
}

// --- Sessions ---

function upsertSession(
  id: string,
  agentId: string,
  agentType: string,
  project?: string,
  branch?: string,
  mode?: 'interactive' | 'headless'
): void {
  const db = getDb();
  // `mode` (invocation mode) is a session-level constant persisted in metadata.
  // When present it is merged into metadata on both insert and conflict so it
  // survives across the many event upserts a session accumulates.
  db.prepare(`
    INSERT INTO sessions (id, agent_id, agent_type, project, branch, metadata)
    VALUES (
      @id, @agentId, @agentType, @project, @branch,
      CASE WHEN @mode IS NOT NULL THEN json_object('mode', @mode) ELSE '{}' END
    )
    ON CONFLICT(id) DO UPDATE SET
      last_event_at = datetime('now'),
      status = 'active',
      ended_at = NULL,
      project = COALESCE(excluded.project, sessions.project),
      branch = COALESCE(excluded.branch, sessions.branch),
      metadata = CASE
        WHEN @mode IS NOT NULL
          THEN json_set(COALESCE(NULLIF(sessions.metadata, ''), '{}'), '$.mode', @mode)
        ELSE sessions.metadata
      END
  `).run({
    id,
    agentId,
    agentType,
    project: project || null,
    branch: branch || null,
    mode: mode ?? null,
  });
}

// Backfill/patch a session's invocation mode independent of event insertion.
// `insertEvent` sets mode via upsertSession, but it returns early for duplicate
// event_ids (and unchanged files are skipped entirely by import_state), so
// sessions imported before mode existed are never updated through that path.
// The import pipeline calls this once per session per file so a re-import
// (including `--force`) backfills mode even when every event is a duplicate.
// Guarded so it is a no-op UPDATE when the mode is already correct.
export function setSessionMode(sessionId: string, mode: 'interactive' | 'headless'): void {
  const db = getDb();
  db.prepare(`
    UPDATE sessions
    SET metadata = json_set(COALESCE(NULLIF(metadata, ''), '{}'), '$.mode', @mode)
    WHERE id = @id
      AND (json_extract(metadata, '$.mode') IS NULL OR json_extract(metadata, '$.mode') != @mode)
  `).run({ id: sessionId, mode });
}

/**
 * Refresh a deterministic imported Codex event after its source JSONL gains an
 * authoritative per-turn model. This is deliberately narrower than a general
 * duplicate upsert: only import rows marked as turn_context-backed may change,
 * and only model plus the derived cost are refreshed.
 */
export function refreshImportedCodexEventModel(
  event: NormalizedIngestEvent,
): { id: number; sessionId: string } | null {
  if (
    !event.event_id
    || event.source !== 'import'
    || event.agent_type !== 'codex'
    || !event.model
    || event.metadata === null
    || typeof event.metadata !== 'object'
    || Array.isArray(event.metadata)
    || (event.metadata as Record<string, unknown>)._model_source !== 'turn_context'
  ) {
    return null;
  }

  const hasUsage = event.tokens_in > 0
    || event.tokens_out > 0
    || (event.cache_read_tokens ?? 0) > 0
    || (event.cache_write_tokens ?? 0) > 0;
  const cost = hasUsage
    ? pricingRegistry.calculate(event.model, {
        input: event.tokens_in,
        output: event.tokens_out,
        cacheRead: event.cache_read_tokens,
        cacheWrite: event.cache_write_tokens,
      })
    : null;

  const row = getDb().prepare(`
    UPDATE events
    SET model = @model,
        cost_usd = CASE WHEN @hasUsage = 1 THEN @cost ELSE cost_usd END
    WHERE event_id = @eventId
      AND source = 'import'
      AND agent_type = 'codex'
      AND (
        model IS NOT @model
        OR (@hasUsage = 1 AND cost_usd IS NOT @cost)
      )
    RETURNING id, session_id
  `).get({
    eventId: event.event_id,
    model: event.model,
    hasUsage: hasUsage ? 1 : 0,
    cost,
  }) as { id: number; session_id: string } | undefined;

  if (row) markStatsDirty();
  return row ? { id: row.id, sessionId: row.session_id } : null;
}

export interface SessionRow {
  id: string;
  agent_id: string;
  agent_type: string;
  project: string | null;
  branch: string | null;
  status: string;
  started_at: string;
  ended_at: string | null;
  last_event_at: string;
  metadata: string;
  event_count: number;
  tokens_in: number;
  tokens_out: number;
  total_cost_usd: number;
  files_edited: number;
  lines_added: number;
  lines_removed: number;
}

export function getSessions(filters: {
  status?: string;
  excludeStatus?: string;
  agentType?: string;
  since?: string;
  limit?: number;
}): SessionRow[] {
  const db = getDb();
  updateIdleSessions(config.sessionTimeoutMinutes);
  const conditions: string[] = [];
  const params: unknown[] = [];

  if (filters.status) {
    conditions.push('s.status = ?');
    params.push(filters.status);
  }
  if (filters.excludeStatus) {
    conditions.push('s.status != ?');
    params.push(filters.excludeStatus);
  }
  if (filters.agentType) {
    conditions.push('s.agent_type = ?');
    params.push(filters.agentType);
  }
  if (filters.since) {
    conditions.push('s.last_event_at >= datetime(?)');
    params.push(filters.since);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  const requestedLimit = Number.isFinite(filters.limit) ? Math.trunc(filters.limit as number) : 50;
  const applyLimit = requestedLimit > 0;
  const queryParams = applyLimit ? [...params, requestedLimit] : params;

  return db.prepare(`
    SELECT s.*,
      COALESCE((SELECT COUNT(*) FROM events e WHERE e.session_id = s.id), 0) as event_count,
      COALESCE((SELECT SUM(e.tokens_in) FROM events e WHERE e.session_id = s.id), 0) as tokens_in,
      COALESCE((SELECT SUM(e.tokens_out) FROM events e WHERE e.session_id = s.id), 0) as tokens_out,
      COALESCE((SELECT SUM(e.cost_usd) FROM events e WHERE e.session_id = s.id), 0) as total_cost_usd,
      COALESCE((SELECT COUNT(DISTINCT json_extract(e.metadata, '$.file_path')) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND e.tool_name IN ('Edit', 'Write', 'MultiEdit', 'apply_patch', 'write_stdin') AND json_extract(e.metadata, '$.file_path') IS NOT NULL), 0) as files_edited,
      COALESCE((SELECT SUM(CAST(json_extract(e.metadata, '$.lines_added') AS INTEGER)) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND json_extract(e.metadata, '$.lines_added') IS NOT NULL), 0) as lines_added,
      COALESCE((SELECT SUM(CAST(json_extract(e.metadata, '$.lines_removed') AS INTEGER)) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND json_extract(e.metadata, '$.lines_removed') IS NOT NULL), 0) as lines_removed
    FROM sessions s
    ${where}
    ORDER BY
      CASE s.status WHEN 'active' THEN 0 WHEN 'idle' THEN 1 ELSE 2 END,
      s.last_event_at DESC
    ${applyLimit ? 'LIMIT ?' : ''}
  `).all(...queryParams) as SessionRow[];
}

export function getSessionWithEvents(sessionId: string, eventLimit: number = 10): {
  session: SessionRow | undefined;
  events: EventRow[];
} {
  const db = getDb();
  const session = db.prepare(`
    SELECT s.*,
      COALESCE((SELECT COUNT(*) FROM events e WHERE e.session_id = s.id), 0) as event_count,
      COALESCE((SELECT SUM(e.tokens_in) FROM events e WHERE e.session_id = s.id), 0) as tokens_in,
      COALESCE((SELECT SUM(e.tokens_out) FROM events e WHERE e.session_id = s.id), 0) as tokens_out,
      COALESCE((SELECT SUM(e.cost_usd) FROM events e WHERE e.session_id = s.id), 0) as total_cost_usd,
      COALESCE((SELECT COUNT(DISTINCT json_extract(e.metadata, '$.file_path')) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND e.tool_name IN ('Edit', 'Write', 'MultiEdit', 'apply_patch', 'write_stdin') AND json_extract(e.metadata, '$.file_path') IS NOT NULL), 0) as files_edited,
      COALESCE((SELECT SUM(CAST(json_extract(e.metadata, '$.lines_added') AS INTEGER)) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND json_extract(e.metadata, '$.lines_added') IS NOT NULL), 0) as lines_added,
      COALESCE((SELECT SUM(CAST(json_extract(e.metadata, '$.lines_removed') AS INTEGER)) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND json_extract(e.metadata, '$.lines_removed') IS NOT NULL), 0) as lines_removed
    FROM sessions s WHERE s.id = ?
  `).get(sessionId) as SessionRow | undefined;

  const events = db.prepare(`
    SELECT * FROM events WHERE session_id = ? ORDER BY created_at DESC LIMIT ?
  `).all(sessionId, eventLimit) as EventRow[];

  return { session, events };
}

export function updateIdleSessions(timeoutMinutes: number): number {
  const db = getDb();
  // Mark active sessions as idle after timeout
  const idled = db.prepare(`
    UPDATE sessions SET status = 'idle'
    WHERE status = 'active'
    AND last_event_at < datetime('now', ? || ' minutes')
  `).run(`-${timeoutMinutes}`);

  // Auto-end idle sessions that remain inactive for an additional timeout window.
  const ended = db.prepare(`
    UPDATE sessions SET status = 'ended', ended_at = datetime('now')
    WHERE status = 'idle'
    AND last_event_at < datetime('now', ? || ' minutes')
  `).run(`-${timeoutMinutes * 2}`);

  if (idled.changes > 0 || ended.changes > 0) {
    markStatsDirty(); // session status counts changed
  }

  return idled.changes;
}

function idleSession(sessionId: string): void {
  const db = getDb();
  db.prepare(`
    UPDATE sessions SET status = 'idle', ended_at = NULL
    WHERE id = ? AND status != 'ended'
  `).run(sessionId);
}

function endSession(sessionId: string): void {
  const db = getDb();
  db.prepare(`
    UPDATE sessions SET status = 'ended', ended_at = datetime('now')
    WHERE id = ?
  `).run(sessionId);
}

// --- Events ---

export interface EventRow {
  id: number;
  event_id: string | null;
  schema_version: number;
  session_id: string;
  agent_type: string;
  event_type: EventType;
  tool_name: string | null;
  status: EventStatus;
  tokens_in: number;
  tokens_out: number;
  branch: string | null;
  project: string | null;
  duration_ms: number | null;
  created_at: string;
  client_timestamp: string | null;
  metadata: string;
  payload_truncated: number;
  model: string | null;
  cost_usd: number | null;
  cache_read_tokens: number;
  cache_write_tokens: number;
  source: EventSource;
}

function isHistoricalImportedEvent(event: {
  source?: string;
  client_timestamp?: string;
}): boolean {
  if (event.source !== 'import') return false;
  if (!event.client_timestamp) return true;

  const clientMs = Date.parse(event.client_timestamp);
  if (Number.isNaN(clientMs)) return true;

  const minutesWindow = Math.max(
    30,
    config.autoImportIntervalMinutes > 0
      ? config.autoImportIntervalMinutes * 3
      : config.sessionTimeoutMinutes * 6,
  );
  return Date.now() - clientMs > minutesWindow * 60_000;
}

const METADATA_PRIORITY_KEYS = [
  'command',
  'file_path',
  'query',
  'pattern',
  'error',
  'message',
  'tool_name',
  'path',
  'type',
];

function safeJsonStringify(value: unknown): string {
  const seen = new WeakSet<object>();
  try {
    return JSON.stringify(value ?? {}, (_key, val) => {
      if (typeof val === 'object' && val !== null) {
        if (seen.has(val)) return '[Circular]';
        seen.add(val);
      }
      return val;
    });
  } catch {
    return '{"_serialization_error":true}';
  }
}

function buildTruncatedObjectSummary(
  metadata: Record<string, unknown>,
  originalBytes: number
): string {
  const summary: Record<string, unknown> = {
    _truncated: true,
    _original_bytes: originalBytes,
  };

  for (const key of METADATA_PRIORITY_KEYS) {
    if (Object.prototype.hasOwnProperty.call(metadata, key)) {
      summary[key] = metadata[key];
    }
  }

  return safeJsonStringify(summary);
}

function buildTruncatedGenericSummary(originalBytes: number): string {
  return safeJsonStringify({
    _truncated: true,
    _original_bytes: originalBytes,
  });
}

function truncateMetadata(metadata: unknown): { value: string; truncated: boolean } {
  const maxBytes = Math.max(0, config.maxPayloadKB * 1024);
  const minJson = '{}';
  const minJsonBytes = Buffer.byteLength(minJson, 'utf8');
  if (maxBytes < minJsonBytes) {
    return { value: minJson, truncated: true };
  }

  const serialized = safeJsonStringify(metadata ?? {});
  const serializedBytes = Buffer.byteLength(serialized, 'utf8');
  if (serializedBytes <= maxBytes) {
    return { value: serialized, truncated: false };
  }

  let summary = buildTruncatedGenericSummary(serializedBytes);
  if (metadata && typeof metadata === 'object' && !Array.isArray(metadata)) {
    summary = buildTruncatedObjectSummary(metadata as Record<string, unknown>, serializedBytes);
  }

  if (Buffer.byteLength(summary, 'utf8') <= maxBytes) {
    return { value: summary, truncated: true };
  }

  const compactSummary = safeJsonStringify({
    _truncated: true,
    _original_bytes: serializedBytes,
  });
  if (Buffer.byteLength(compactSummary, 'utf8') <= maxBytes) {
    return { value: compactSummary, truncated: true };
  }

  return {
    value: minJson,
    truncated: true,
  };
}

export function insertEvent(event: {
  event_id?: string;
  session_id: string;
  agent_type: string;
  event_type: EventType;
  tool_name?: string;
  status: EventStatus;
  tokens_in: number;
  tokens_out: number;
  branch?: string;
  project?: string;
  duration_ms?: number;
  metadata: unknown;
  client_timestamp?: string;
  model?: string;
  cost_usd?: number | null;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  source?: string;
  mode?: 'interactive' | 'headless';
}): EventRow | null {
  const db = getDb();
  const isHistoricalImport = isHistoricalImportedEvent(event);
  if (event.event_id) {
    const existing = db.prepare('SELECT id FROM events WHERE event_id = ?').get(event.event_id) as { id: number } | undefined;
    if (existing) return null;
  }

  const agentId = `${event.agent_type}-default`;
  upsertAgent(agentId, event.agent_type);
  upsertSession(event.session_id, agentId, event.agent_type, event.project, event.branch, event.mode);

  // Backfill project/branch from session if missing on this event
  if (!event.project || !event.branch) {
    const session = db.prepare('SELECT project, branch FROM sessions WHERE id = ?').get(event.session_id) as { project: string | null; branch: string | null } | undefined;
    if (session) {
      if (!event.project && session.project) event.project = session.project;
      if (!event.branch && session.branch) event.branch = session.branch;
    }
  }

  // Resolve git branch from project directory and keep session branch fresh.
  // Recent live imports can carry stale branch metadata from session start, so
  // refresh the session-level branch from current repo HEAD when possible.
  if (event.project && (event.source !== 'import' || !isHistoricalImport)) {
    const gitBranch = resolveGitBranch(event.project);
    if (gitBranch) {
      if (!event.branch) {
        event.branch = gitBranch;
      }
      db.prepare(`
        UPDATE sessions
        SET branch = ?
        WHERE id = ? AND (branch IS NULL OR branch != ?)
      `).run(gitBranch, event.session_id, gitBranch);
    }
  }

  // Auto-calculate cost if model + tokens present but cost not provided
  if (event.model && (event.tokens_in > 0 || event.tokens_out > 0)) {
    if (event.cost_usd === undefined || event.cost_usd === null) {
      event.cost_usd = pricingRegistry.calculate(event.model, {
        input: event.tokens_in,
        output: event.tokens_out,
        cacheRead: event.cache_read_tokens,
        cacheWrite: event.cache_write_tokens,
      });
    }
  }

  const metadata = truncateMetadata(event.metadata);

  try {
    const result = db.prepare(`
      INSERT INTO events (event_id, session_id, agent_type, event_type, tool_name, status,
        tokens_in, tokens_out, branch, project, duration_ms, created_at, client_timestamp,
        metadata, payload_truncated, model, cost_usd, cache_read_tokens, cache_write_tokens, source)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      event.event_id || null,
      event.session_id,
      event.agent_type,
      event.event_type,
      event.tool_name || null,
      event.status,
      event.tokens_in,
      event.tokens_out,
      event.branch || null,
      event.project || null,
      event.duration_ms || null,
      event.client_timestamp || null,
      metadata.value,
      metadata.truncated ? 1 : 0,
      event.model || null,
      event.cost_usd ?? null,
      event.cache_read_tokens ?? 0,
      event.cache_write_tokens ?? 0,
      event.source || 'api'
    );

    if (result.changes === 0) return null; // duplicate event_id

    // Handle session lifecycle for successful inserts only.
    // Recent import session_end events are often synthetic snapshots, so they
    // should not force a live session to ended.
    if (event.event_type === 'session_end' && (event.source !== 'import' || isHistoricalImport)) {
      // Claude Code sessions go to 'idle' first so they linger on the dashboard
      // for one timeout cycle before the periodic cleanup marks them 'ended'.
      if (event.agent_type === 'claude_code') {
        idleSession(event.session_id);
      } else {
        endSession(event.session_id);
      }
    }

    // Keep clearly historical imports out of active lists.
    if (isHistoricalImport) {
      db.prepare(`
        UPDATE sessions SET status = 'ended', ended_at = COALESCE(ended_at, datetime('now'))
        WHERE id = ? AND status != 'ended'
      `).run(event.session_id);
    }

    const row = db.prepare('SELECT * FROM events WHERE id = ?').get(result.lastInsertRowid) as EventRow;

    if (row.agent_type === 'codex') {
      syncCodexSummaryLiveEvent(db, row);
    }

    markStatsDirty(); // event (and its session-status side effects) changed totals
    return row;
  } catch (err: unknown) {
    // UNIQUE constraint violation = duplicate event_id, silently skip
    if (err instanceof Error && err.message.includes('UNIQUE constraint failed: events.event_id')) {
      return null;
    }
    throw err;
  }
}

export function getEvents(filters: {
  limit?: number;
  offset?: number;
  agentType?: string;
  eventType?: string;
  toolName?: string;
  sessionId?: string;
  branch?: string;
  model?: string;
  source?: string;
  since?: string;
  until?: string;
}): { events: EventRow[]; total: number } {
  const db = getDb();
  const conditions: string[] = [];
  const params: unknown[] = [];

  if (filters.agentType) {
    conditions.push('agent_type = ?');
    params.push(filters.agentType);
  }
  if (filters.eventType) {
    conditions.push('event_type = ?');
    params.push(filters.eventType);
  }
  if (filters.toolName) {
    conditions.push('tool_name = ?');
    params.push(filters.toolName);
  }
  if (filters.sessionId) {
    conditions.push('session_id = ?');
    params.push(filters.sessionId);
  }
  if (filters.branch) {
    conditions.push('branch = ?');
    params.push(filters.branch);
  }
  if (filters.model) {
    conditions.push('model = ?');
    params.push(filters.model);
  }
  if (filters.source) {
    conditions.push('source = ?');
    params.push(filters.source);
  }
  if (filters.since) {
    conditions.push('created_at >= datetime(?)');
    params.push(filters.since);
  }
  if (filters.until) {
    conditions.push('created_at <= datetime(?)');
    params.push(filters.until);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  const limit = filters.limit || 50;
  const offset = filters.offset || 0;

  const total = (db.prepare(`SELECT COUNT(*) as count FROM events ${where}`).get(...params) as { count: number }).count;
  const events = db.prepare(`
    SELECT * FROM events ${where} ORDER BY created_at DESC LIMIT ? OFFSET ?
  `).all(...params, limit, offset) as EventRow[];

  return { events, total };
}

export function listTraceQualityEventSourcesForSession(sessionId: string): EventRow[] {
  const db = getDb();
  return db.prepare(`
    SELECT *
    FROM events
    WHERE session_id = ?
    ORDER BY COALESCE(client_timestamp, created_at), id
  `).all(sessionId) as EventRow[];
}

// --- Stats ---

export interface Stats {
  total_events: number;
  active_sessions: number;
  total_sessions: number;
  live_sessions: number;
  active_agents: number;
  total_tokens_in: number;
  total_tokens_out: number;
  total_cost_usd: number;
  tool_breakdown: Record<string, number>;
  agent_breakdown: Record<string, number>;
  model_breakdown: Record<string, number>;
  branches: string[];
}

export function getStats(filters?: { agentType?: string; since?: string }): Stats {
  const db = getDb();
  updateIdleSessions(config.sessionTimeoutMinutes);
  const conditions: string[] = [];
  const params: unknown[] = [];

  if (filters?.agentType) {
    conditions.push('e.agent_type = ?');
    params.push(filters.agentType);
  }
  if (filters?.since) {
    conditions.push('e.created_at >= datetime(?)');
    params.push(filters.since);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

  const totals = db.prepare(`
    SELECT
      COUNT(*) as total_events,
      ${reconciledUsageSum('e', 'tokens_in')} as total_tokens_in,
      ${reconciledUsageSum('e', 'tokens_out')} as total_tokens_out,
      ${reconciledUsageSum('e', 'cost_usd')} as total_cost_usd
    FROM events e ${where}
  `).get(...params) as { total_events: number; total_tokens_in: number; total_tokens_out: number; total_cost_usd: number };

  const activeSessions = (db.prepare(
    `SELECT COUNT(*) as count FROM sessions WHERE status = 'active'`
  ).get() as { count: number }).count;

  const totalSessions = (db.prepare(
    `SELECT COUNT(*) as count FROM sessions`
  ).get() as { count: number }).count;
  const liveSessions = (db.prepare(
    `SELECT COUNT(*) as count FROM sessions WHERE status != 'ended'`
  ).get() as { count: number }).count;
  const activeAgents = (db.prepare(
    `SELECT COUNT(DISTINCT agent_type) as count FROM sessions WHERE status != 'ended'`
  ).get() as { count: number }).count;

  const toolRows = db.prepare(`
    SELECT tool_name, COUNT(*) as count FROM events e
    ${where.replace('WHERE', conditions.length ? 'WHERE' : '')}
    ${conditions.length > 0 ? 'AND' : 'WHERE'} tool_name IS NOT NULL
    GROUP BY tool_name ORDER BY count DESC
  `).all(...params) as { tool_name: string; count: number }[];

  const toolBreakdown: Record<string, number> = {};
  for (const row of toolRows) {
    toolBreakdown[row.tool_name] = row.count;
  }

  const agentRows = db.prepare(`
    SELECT agent_type, COUNT(*) as count FROM events e ${where}
    GROUP BY agent_type ORDER BY count DESC
  `).all(...params) as { agent_type: string; count: number }[];

  const agentBreakdown: Record<string, number> = {};
  for (const row of agentRows) {
    agentBreakdown[row.agent_type] = row.count;
  }

  const modelRows = db.prepare(`
    SELECT model, COUNT(*) as count FROM events e
    ${where.replace('WHERE', conditions.length ? 'WHERE' : '')}
    ${conditions.length > 0 ? 'AND' : 'WHERE'} model IS NOT NULL
    GROUP BY model ORDER BY count DESC
  `).all(...params) as { model: string; count: number }[];

  const modelBreakdown: Record<string, number> = {};
  for (const row of modelRows) {
    modelBreakdown[row.model] = row.count;
  }

  const branchRows = db.prepare(`
    SELECT DISTINCT branch FROM sessions WHERE branch IS NOT NULL ORDER BY last_event_at DESC
  `).all() as { branch: string }[];

  return {
    total_events: totals.total_events,
    active_sessions: activeSessions,
    total_sessions: totalSessions,
    live_sessions: liveSessions,
    active_agents: activeAgents,
    total_tokens_in: totals.total_tokens_in,
    total_tokens_out: totals.total_tokens_out,
    total_cost_usd: totals.total_cost_usd,
    tool_breakdown: toolBreakdown,
    agent_breakdown: agentBreakdown,
    model_breakdown: modelBreakdown,
    branches: branchRows.map(r => r.branch),
  };
}

// Cached unfiltered stats for the periodic SSE broadcast.
//
// getStats() runs several full-table events aggregates (including the correlated
// Codex usage-reconciliation sum). The stats broadcaster fires every
// statsIntervalMs (default 5s) for as long as a dashboard tab is open, but the
// underlying totals only change when events are ingested or session status
// shifts. Recomputing the whole snapshot on every tick is pure waste, so we
// memoize the unfiltered result and recompute lazily only after a write marks it
// dirty (see markStatsDirty). The filtered getStats() path (HTTP /api/stats) is
// on-demand and stays uncached.
let cachedBroadcastStats: Stats | null = null;
let broadcastStatsDirty = true;

function markStatsDirty(): void {
  broadcastStatsDirty = true;
}

export function getStatsForBroadcast(): Stats {
  if (broadcastStatsDirty || cachedBroadcastStats === null) {
    cachedBroadcastStats = getStats();
    broadcastStatsDirty = false;
  }
  return cachedBroadcastStats;
}

// --- Provider Quotas ---

export type ProviderName = 'claude' | 'codex';

export interface ProviderQuotaWindow {
  used_percent: number;
  remaining_percent: number;
  resets_at: string | null;
  window_minutes: number | null;
}

export interface ProviderQuotaCredits {
  has_credits: boolean;
  unlimited: boolean;
  balance: string | null;
}

export interface ProviderQuotaSnapshot {
  provider: ProviderName;
  agent_type: 'claude_code' | 'codex';
  status: 'available' | 'unavailable' | 'error';
  source: string | null;
  updated_at: string | null;
  account_label: string | null;
  plan_type: string | null;
  limit_id: string | null;
  limit_name: string | null;
  error_message: string | null;
  primary: ProviderQuotaWindow | null;
  secondary: ProviderQuotaWindow | null;
  credits: ProviderQuotaCredits | null;
}

interface ProviderQuotaWindowInput {
  used_percent?: number | null;
  resets_at?: string | number | null;
  window_minutes?: number | null;
}

export interface ProviderQuotaSnapshotInput {
  provider: ProviderName;
  agent_type?: 'claude_code' | 'codex';
  status?: ProviderQuotaSnapshot['status'];
  source?: string | null;
  updated_at?: string | null;
  account_label?: string | null;
  plan_type?: string | null;
  limit_id?: string | null;
  limit_name?: string | null;
  error_message?: string | null;
  primary?: ProviderQuotaWindowInput | null;
  secondary?: ProviderQuotaWindowInput | null;
  credits?: {
    has_credits?: boolean | null;
    unlimited?: boolean | null;
    balance?: string | null;
  } | null;
  raw_payload?: unknown;
}

const PROVIDER_DEFAULTS: Record<ProviderName, Pick<ProviderQuotaSnapshot, 'agent_type' | 'status' | 'source' | 'updated_at' | 'account_label' | 'plan_type' | 'limit_id' | 'limit_name' | 'error_message' | 'primary' | 'secondary' | 'credits'>> = {
  claude: {
    agent_type: 'claude_code',
    status: 'unavailable',
    source: null,
    updated_at: null,
    account_label: null,
    plan_type: null,
    limit_id: null,
    limit_name: null,
    error_message: null,
    primary: null,
    secondary: null,
    credits: null,
  },
  codex: {
    agent_type: 'codex',
    status: 'unavailable',
    source: null,
    updated_at: null,
    account_label: null,
    plan_type: null,
    limit_id: null,
    limit_name: null,
    error_message: null,
    primary: null,
    secondary: null,
    credits: null,
  },
};

function normalizeQuotaReset(value: string | number | null | undefined): string | null {
  if (value == null || value === '') return null;
  if (typeof value === 'number' && Number.isFinite(value)) {
    return new Date(value * 1000).toISOString();
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed === '') return null;
    const numeric = Number(trimmed);
    if (Number.isFinite(numeric) && trimmed === String(numeric)) {
      return new Date(numeric * 1000).toISOString();
    }
    const parsed = Date.parse(trimmed);
    return Number.isNaN(parsed) ? null : new Date(parsed).toISOString();
  }
  return null;
}

function normalizeQuotaWindow(input: ProviderQuotaWindowInput | null | undefined): ProviderQuotaWindow | null {
  if (!input) return null;
  if (input.used_percent == null || !Number.isFinite(input.used_percent)) return null;
  const usedPercent = Math.max(0, Math.min(input.used_percent, 100));
  return {
    used_percent: usedPercent,
    remaining_percent: Math.max(0, Math.min(100 - usedPercent, 100)),
    resets_at: normalizeQuotaReset(input.resets_at),
    window_minutes: input.window_minutes == null || !Number.isFinite(input.window_minutes)
      ? null
      : Math.max(0, Math.round(input.window_minutes)),
  };
}

function normalizeQuotaTimestamp(value: string | null | undefined): string {
  if (value) {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) return new Date(parsed).toISOString();
  }
  return new Date().toISOString();
}

export function upsertProviderQuotaSnapshot(input: ProviderQuotaSnapshotInput): void {
  const db = getDb();
  const defaults = PROVIDER_DEFAULTS[input.provider];
  const primary = normalizeQuotaWindow(input.primary);
  const secondary = normalizeQuotaWindow(input.secondary);
  const credits = input.credits
    ? {
        has_credits: Boolean(input.credits.has_credits),
        unlimited: Boolean(input.credits.unlimited),
        balance: input.credits.balance ?? null,
      }
    : null;

  db.prepare(`
    INSERT INTO provider_quotas (
      provider, agent_type, status, source, updated_at, account_label, plan_type,
      limit_id, limit_name, error_message, primary_used_percent, primary_window_minutes,
      primary_resets_at, secondary_used_percent, secondary_window_minutes,
      secondary_resets_at, credits_has_credits, credits_unlimited, credits_balance, raw_payload
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(provider) DO UPDATE SET
      agent_type = excluded.agent_type,
      status = excluded.status,
      source = excluded.source,
      updated_at = excluded.updated_at,
      account_label = excluded.account_label,
      plan_type = excluded.plan_type,
      limit_id = excluded.limit_id,
      limit_name = excluded.limit_name,
      error_message = excluded.error_message,
      primary_used_percent = excluded.primary_used_percent,
      primary_window_minutes = excluded.primary_window_minutes,
      primary_resets_at = excluded.primary_resets_at,
      secondary_used_percent = excluded.secondary_used_percent,
      secondary_window_minutes = excluded.secondary_window_minutes,
      secondary_resets_at = excluded.secondary_resets_at,
      credits_has_credits = excluded.credits_has_credits,
      credits_unlimited = excluded.credits_unlimited,
      credits_balance = excluded.credits_balance,
      raw_payload = excluded.raw_payload
  `).run(
    input.provider,
    input.agent_type ?? defaults.agent_type,
    input.status ?? (primary || secondary ? 'available' : defaults.status),
    input.source ?? defaults.source,
    normalizeQuotaTimestamp(input.updated_at),
    input.account_label ?? defaults.account_label,
    input.plan_type ?? defaults.plan_type,
    input.limit_id ?? defaults.limit_id,
    input.limit_name ?? defaults.limit_name,
    input.error_message ?? defaults.error_message,
    primary?.used_percent ?? null,
    primary?.window_minutes ?? null,
    primary?.resets_at ?? null,
    secondary?.used_percent ?? null,
    secondary?.window_minutes ?? null,
    secondary?.resets_at ?? null,
    credits ? (credits.has_credits ? 1 : 0) : null,
    credits ? (credits.unlimited ? 1 : 0) : null,
    credits?.balance ?? null,
    input.raw_payload == null ? null : JSON.stringify(input.raw_payload),
  );
}

export function getProviderQuotas(): ProviderQuotaSnapshot[] {
  const db = getDb();
  const rows = db.prepare(`
    SELECT
      provider, agent_type, status, source, updated_at, account_label, plan_type,
      limit_id, limit_name, error_message, primary_used_percent, primary_window_minutes,
      primary_resets_at, secondary_used_percent, secondary_window_minutes,
      secondary_resets_at, credits_has_credits, credits_unlimited, credits_balance
    FROM provider_quotas
  `).all() as Array<{
    provider: ProviderName;
    agent_type: 'claude_code' | 'codex';
    status: ProviderQuotaSnapshot['status'];
    source: string | null;
    updated_at: string | null;
    account_label: string | null;
    plan_type: string | null;
    limit_id: string | null;
    limit_name: string | null;
    error_message: string | null;
    primary_used_percent: number | null;
    primary_window_minutes: number | null;
    primary_resets_at: string | null;
    secondary_used_percent: number | null;
    secondary_window_minutes: number | null;
    secondary_resets_at: string | null;
    credits_has_credits: number | null;
    credits_unlimited: number | null;
    credits_balance: string | null;
  }>;

  const byProvider = new Map(rows.map((row) => [row.provider, row]));

  return (['claude', 'codex'] as const).map((provider) => {
    const row = byProvider.get(provider);
    if (!row) {
      return { provider, ...PROVIDER_DEFAULTS[provider] };
    }

    return {
      provider,
      agent_type: row.agent_type,
      status: row.status,
      source: row.source,
      updated_at: row.updated_at,
      account_label: row.account_label,
      plan_type: row.plan_type,
      limit_id: row.limit_id,
      limit_name: row.limit_name,
      error_message: row.error_message,
      primary: normalizeQuotaWindow({
        used_percent: row.primary_used_percent,
        resets_at: row.primary_resets_at,
        window_minutes: row.primary_window_minutes,
      }),
      secondary: normalizeQuotaWindow({
        used_percent: row.secondary_used_percent,
        resets_at: row.secondary_resets_at,
        window_minutes: row.secondary_window_minutes,
      }),
      credits: row.credits_has_credits == null && row.credits_unlimited == null && row.credits_balance == null
        ? null
        : {
            has_credits: Boolean(row.credits_has_credits),
            unlimited: Boolean(row.credits_unlimited),
            balance: row.credits_balance,
          },
    };
  });
}

// --- Filter Options ---

export interface FilterOptions {
  agent_types: string[];
  event_types: string[];
  tool_names: string[];
  models: string[];
  projects: string[];
  branches: Array<{ value: string; label: string }>;
  sources: string[];
}

export function getFilterOptions(): FilterOptions {
  const db = getDb();

  const agentTypes = (db.prepare(
    'SELECT DISTINCT agent_type FROM events WHERE agent_type IS NOT NULL ORDER BY agent_type'
  ).all() as { agent_type: string }[]).map(r => r.agent_type);

  const eventTypes = (db.prepare(
    'SELECT DISTINCT event_type FROM events WHERE event_type IS NOT NULL ORDER BY event_type'
  ).all() as { event_type: string }[]).map(r => r.event_type);

  const toolNames = (db.prepare(
    'SELECT DISTINCT tool_name FROM events WHERE tool_name IS NOT NULL ORDER BY tool_name'
  ).all() as { tool_name: string }[]).map(r => r.tool_name);

  const models = (db.prepare(
    'SELECT DISTINCT model FROM events WHERE model IS NOT NULL ORDER BY model'
  ).all() as { model: string }[]).map(r => r.model);

  const projects = (db.prepare(
    'SELECT DISTINCT project FROM sessions WHERE project IS NOT NULL ORDER BY project'
  ).all() as { project: string }[]).map(r => r.project);

  const branchData = db.prepare(
    `SELECT branch, project, MAX(last_event_at) as latest
     FROM sessions
     WHERE branch IS NOT NULL AND branch != 'HEAD'
     GROUP BY branch
     ORDER BY latest DESC`
  ).all() as { branch: string; project: string | null; latest: string }[];
  const branches = branchData.map(r => ({
    value: r.branch,
    label: r.project ? `${r.project} / ${r.branch}` : r.branch,
  }));

  const sources = (db.prepare(
    'SELECT DISTINCT source FROM events WHERE source IS NOT NULL ORDER BY source'
  ).all() as { source: string }[]).map(r => r.source);

  return { agent_types: agentTypes, event_types: eventTypes, tool_names: toolNames, models, projects, branches, sources };
}

// --- Tool Analytics ---

export interface ToolAnalyticsRow {
  tool_name: string;
  total_calls: number;
  error_count: number;
  error_rate: number;
  avg_duration_ms: number | null;
  by_agent: Record<string, number>;
}

export function getToolAnalytics(filters?: { agentType?: string; since?: string }): ToolAnalyticsRow[] {
  const db = getDb();
  const conditions: string[] = ['tool_name IS NOT NULL'];
  const params: unknown[] = [];

  if (filters?.agentType) {
    conditions.push('agent_type = ?');
    params.push(filters.agentType);
  }
  if (filters?.since) {
    conditions.push('created_at >= datetime(?)');
    params.push(filters.since);
  }

  const where = `WHERE ${conditions.join(' AND ')}`;

  const rows = db.prepare(`
    SELECT
      tool_name,
      COUNT(*) as total_calls,
      SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count,
      ROUND(CAST(SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS REAL) / COUNT(*), 4) as error_rate,
      ROUND(AVG(duration_ms)) as avg_duration_ms
    FROM events ${where}
    GROUP BY tool_name
    ORDER BY total_calls DESC
  `).all(...params) as Array<{
    tool_name: string;
    total_calls: number;
    error_count: number;
    error_rate: number;
    avg_duration_ms: number | null;
  }>;

  // Get per-agent breakdown for each tool
  const agentRows = db.prepare(`
    SELECT tool_name, agent_type, COUNT(*) as count
    FROM events ${where}
    GROUP BY tool_name, agent_type
    ORDER BY tool_name, count DESC
  `).all(...params) as Array<{ tool_name: string; agent_type: string; count: number }>;

  const agentMap = new Map<string, Record<string, number>>();
  for (const r of agentRows) {
    if (!agentMap.has(r.tool_name)) agentMap.set(r.tool_name, {});
    agentMap.get(r.tool_name)![r.agent_type] = r.count;
  }

  return rows.map(r => ({
    tool_name: r.tool_name,
    total_calls: r.total_calls,
    error_count: r.error_count,
    error_rate: r.error_rate,
    avg_duration_ms: r.avg_duration_ms,
    by_agent: agentMap.get(r.tool_name) || {},
  }));
}

// --- Cost over time (hourly buckets) ---

export interface CostBucket {
  bucket: string;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  event_count: number;
}

export function getCostOverTime(filters?: { since?: string; agentType?: string }): CostBucket[] {
  const db = getDb();
  const conditions: string[] = [];
  const params: unknown[] = [];

  if (filters?.agentType) {
    conditions.push('e.agent_type = ?');
    params.push(filters.agentType);
  }
  if (filters?.since) {
    conditions.push('datetime(COALESCE(e.client_timestamp, e.created_at)) >= datetime(?)');
    params.push(filters.since);
  }
  conditions.push(excludeOverlappingCodexOtelUsageCondition('e'));

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

  return db.prepare(`
    SELECT
      strftime('%Y-%m-%dT%H:00:00Z', COALESCE(e.client_timestamp, e.created_at)) as bucket,
      COALESCE(SUM(e.cost_usd), 0) as cost_usd,
      COALESCE(SUM(e.tokens_in), 0) as tokens_in,
      COALESCE(SUM(e.tokens_out), 0) as tokens_out,
      COUNT(*) as event_count
    FROM events e ${where}
    GROUP BY bucket
    ORDER BY bucket ASC
  `).all(...params) as CostBucket[];
}

// --- Cost by session (top sessions) ---

export interface ProjectCostRow {
  project: string;
  cost_usd: number;
  session_count: number;
  event_count: number;
}

export function getCostByProject(limit: number = 10, filters?: { agentType?: string; since?: string }): ProjectCostRow[] {
  const db = getDb();
  const conditions: string[] = ['e.cost_usd > 0', excludeOverlappingCodexOtelUsageCondition('e')];
  const params: unknown[] = [];

  if (filters?.agentType) {
    conditions.push('e.agent_type = ?');
    params.push(filters.agentType);
  }
  if (filters?.since) {
    conditions.push('e.created_at >= datetime(?)');
    params.push(filters.since);
  }

  const where = `WHERE ${conditions.join(' AND ')}`;

  return db.prepare(`
    SELECT
      COALESCE(s.project, 'unknown') as project,
      COALESCE(SUM(e.cost_usd), 0) as cost_usd,
      COUNT(DISTINCT e.session_id) as session_count,
      COUNT(*) as event_count
    FROM events e
    LEFT JOIN sessions s ON s.id = e.session_id
    ${where}
    GROUP BY s.project
    ORDER BY cost_usd DESC
    LIMIT ?
  `).all(...params, limit) as ProjectCostRow[];
}

// --- Cost by model ---

export interface ModelCostRow {
  model: string;
  cost_usd: number;
  event_count: number;
  tokens_in: number;
  tokens_out: number;
}

export function getCostByModel(filters?: { agentType?: string; since?: string }): ModelCostRow[] {
  const db = getDb();
  const conditions: string[] = ['e.model IS NOT NULL', 'e.cost_usd > 0', excludeOverlappingCodexOtelUsageCondition('e')];
  const params: unknown[] = [];

  if (filters?.agentType) {
    conditions.push('e.agent_type = ?');
    params.push(filters.agentType);
  }
  if (filters?.since) {
    conditions.push('e.created_at >= datetime(?)');
    params.push(filters.since);
  }

  const where = `WHERE ${conditions.join(' AND ')}`;

  return db.prepare(`
    SELECT
      e.model,
      COALESCE(SUM(e.cost_usd), 0) as cost_usd,
      COUNT(*) as event_count,
      COALESCE(SUM(e.tokens_in), 0) as tokens_in,
      COALESCE(SUM(e.tokens_out), 0) as tokens_out
    FROM events e
    ${where}
    GROUP BY e.model
    ORDER BY cost_usd DESC
  `).all(...params) as ModelCostRow[];
}

// --- Session Transcript ---

export interface TranscriptEvent {
  id: number;
  event_type: string;
  tool_name: string | null;
  status: string;
  tokens_in: number;
  tokens_out: number;
  model: string | null;
  cost_usd: number | null;
  duration_ms: number | null;
  created_at: string;
  client_timestamp: string | null;
  metadata: string;
}

export function getSessionTranscript(sessionId: string): TranscriptEvent[] {
  const db = getDb();
  return db.prepare(`
    SELECT id, event_type, tool_name, status, tokens_in, tokens_out,
           model, cost_usd, duration_ms, created_at, client_timestamp, metadata
    FROM events
    WHERE session_id = ?
    ORDER BY created_at ASC, id ASC
  `).all(sessionId) as TranscriptEvent[];
}
