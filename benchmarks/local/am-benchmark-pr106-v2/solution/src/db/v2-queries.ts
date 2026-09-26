import type { Database } from 'better-sqlite3';
import { getDb } from './connection.js';
import { config } from '../config.js';
import {
  scanSkillCatalogs,
  refreshCatalogSnapshots,
  resolveVersionAt,
  type CatalogSkill,
  type CatalogSnapshot,
} from '../skills/catalog.js';
import type {
  BrowsingSessionRow,
  BrowsingSessionDbRow,
  LiveSessionRow,
  LiveTurnRow,
  LiveItemRow,
  MessageRow,
  ToolCallRow,
  SessionActivity,
  SessionActivityBucket,
  PinnedMessageRow,
  CountResult,
  SessionsListParams,
  MessagesListParams,
  LiveSessionsListParams,
  LiveItemsListParams,
  SearchParams,
  AnalyticsParams,
  AnalyticsSummary,
  ActivityDataPoint,
  ProjectBreakdown,
  ToolUsageStat,
  MonitorToolStat,
  MonitorSessionRow,
  MonitorEventRow,
  MonitorQuotaSnapshot,
  MonitorStats,
  MonitorFilterOptions,
  MonitorTranscriptEvent,
  MonitorTranscriptEntry,
  MonitorTranscriptRow,
  SkillUsageDay,
  SkillHealthRow,
  SkillConsultationAnalytics,
  AnalyticsCoverage,
  HourOfWeekDataPoint,
  TopSessionStat,
  VelocityMetrics,
  AgentComparisonRow,
  UsageParams,
  UsageCoverage,
  UsageSourceBreakdown,
  UsageSummary,
  UsageDailyPoint,
  UsageProjectBreakdown,
  UsageModelBreakdown,
  UsageModelDailyPoint,
  UsageOverview,
  UsageFacets,
  UsageTierBreakdown,
  UsageAgentBreakdown,
  UsageTopSessionRow,
  MonitorSessionsParams,
  MonitorEventsParams,
  MonitorStatsParams,
  InsightRow,
  InsightDbRow,
  InsightInputSnapshot,
  InsightsListParams,
  GenerateInsightParams,
  SearchResultRow,
  PinsListParams,
  BenchmarkStudySummary,
  BenchmarkStudyDetail,
  BenchmarkArm,
  BenchmarkCostBasis,
} from '../api/v2/types.js';
import { inferProjectionCapabilities } from '../live/projector.js';
import { pricingRegistry } from '../pricing/index.js';
import { computeOccupancy } from '../pricing/context-windows.js';
import { classifyModelForUsage, type ModelClassification } from '../pricing/model-classification.js';
import { excludeBenchmarkUsageCondition, excludeOverlappingCodexOtelUsageCondition, reconciledUsageSum } from './usage-reconciliation.js';
import { selectSkillInvocationOccurrences } from '../skills/invocation-ledger.js';
import { getSkillConsultationAnalytics } from '../skills/consultation-analytics.js';

function mapBrowsingSessionRow(row: BrowsingSessionDbRow): BrowsingSessionRow {
  return {
    id: row.id,
    project: row.project,
    agent: row.agent,
    first_message: row.first_message,
    started_at: row.started_at,
    ended_at: row.ended_at,
    message_count: row.message_count,
    user_message_count: row.user_message_count,
    parent_session_id: row.parent_session_id,
    relationship_type: row.relationship_type,
    live_status: row.live_status,
    last_item_at: row.last_item_at,
    integration_mode: row.integration_mode,
    fidelity: row.fidelity,
    capabilities: inferProjectionCapabilities({
      capabilities_json: row.capabilities_json,
      fidelity: row.fidelity,
      integration_mode: row.integration_mode,
    }),
    file_path: row.file_path,
    file_size: row.file_size,
    file_hash: row.file_hash,
    context_used_tokens: row.context_used_tokens ?? null,
    context_window_tokens: row.context_window_tokens ?? null,
    context_pct: computeOccupancy({
      usedTokens: row.context_used_tokens,
      window: row.context_window_tokens,
    })?.pct ?? null,
  };
}

// --- Sessions ---

interface SessionsResult {
  data: BrowsingSessionRow[];
  total: number;
  cursor?: string;
}

interface TimeCursor {
  sort_at: string;
  id: string;
}

function encodeTimeCursor(cursor: TimeCursor): string {
  return Buffer.from(JSON.stringify(cursor), 'utf-8').toString('base64url');
}

function decodeTimeCursor(cursor: string | undefined): TimeCursor | null {
  if (!cursor) return null;

  try {
    const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf-8')) as Partial<TimeCursor>;
    if (typeof parsed.sort_at === 'string' && typeof parsed.id === 'string') {
      return { sort_at: parsed.sort_at, id: parsed.id };
    }
  } catch {
    // Fall back to legacy timestamp-only cursors below.
  }

  return { sort_at: cursor, id: '\uffff' };
}

export function listBrowsingSessions(params: SessionsListParams = {}): SessionsResult {
  const db = getDb();
  const limit = Math.min(Math.max(params.limit ?? 200, 1), 500);
  const conditions: string[] = [];
  const values: unknown[] = [];

  if (params.project) {
    conditions.push('project = ?');
    values.push(params.project);
  }
  if (params.agent) {
    conditions.push('agent = ?');
    values.push(params.agent);
  }
  if (params.date_from) {
    conditions.push('started_at >= ?');
    values.push(params.date_from);
  }
  if (params.date_to) {
    // Include the full day
    conditions.push('started_at < ?');
    const nextDay = new Date(params.date_to);
    nextDay.setDate(nextDay.getDate() + 1);
    values.push(nextDay.toISOString().split('T')[0]);
  }
  if (params.min_messages != null) {
    conditions.push('message_count >= ?');
    values.push(params.min_messages);
  }
  if (params.max_messages != null) {
    conditions.push('message_count <= ?');
    values.push(params.max_messages);
  }
  if (params.exclude_empty) {
    // Hide telemetry-only sessions that have no browsable transcript — opening
    // them shows nothing. The effective history capability mirrors what the
    // client computes via inferProjectionCapabilities; 'unknown' is kept.
    conditions.push(`${analyticsCapabilityExpr('history')} != 'none'`);
  }
  const filterWhere = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  const filterValues = [...values];

  const total = (db.prepare(
    `SELECT COUNT(*) as c FROM browsing_sessions ${filterWhere}`
  ).get(...filterValues) as CountResult).c;

  const cursor = decodeTimeCursor(params.cursor);
  if (cursor) {
    conditions.push('(started_at < ? OR (started_at = ? AND id < ?))');
    values.push(cursor.sort_at, cursor.sort_at, cursor.id);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';

  values.push(limit);
  const data = (db.prepare(
    `SELECT * FROM browsing_sessions ${where} ORDER BY started_at DESC, id DESC LIMIT ?`
  ).all(...values) as BrowsingSessionDbRow[]).map(mapBrowsingSessionRow);

  // Build cursor from last item
  let nextCursor: string | undefined;
  if (data.length === limit && data.length > 0) {
    const last = data[data.length - 1];
    if (last.started_at) {
      nextCursor = encodeTimeCursor({ sort_at: last.started_at, id: last.id });
    }
  }

  return { data, total, cursor: nextCursor };
}

export function getBrowsingSession(id: string): BrowsingSessionRow | undefined {
  const db = getDb();
  const row = db.prepare('SELECT * FROM browsing_sessions WHERE id = ?').get(id) as BrowsingSessionDbRow | undefined;
  return row ? mapBrowsingSessionRow(row) : undefined;
}

export function getSessionChildren(parentId: string): BrowsingSessionRow[] {
  const db = getDb();
  return (db.prepare(
    'SELECT * FROM browsing_sessions WHERE parent_session_id = ? ORDER BY started_at'
  ).all(parentId) as BrowsingSessionDbRow[]).map(mapBrowsingSessionRow);
}

// --- Live sessions ---

interface LiveSessionsResult {
  data: LiveSessionRow[];
  total: number;
  cursor?: string;
}

export function listLiveSessions(params: LiveSessionsListParams = {}): LiveSessionsResult {
  const db = getDb();
  const limit = Math.min(Math.max(params.limit ?? 200, 1), 500);
  const conditions: string[] = [];
  const values: unknown[] = [];

  if (params.project) {
    conditions.push('project = ?');
    values.push(params.project);
  }
  if (params.agent) {
    conditions.push('agent = ?');
    values.push(params.agent);
  }
  if (params.live_status) {
    conditions.push('live_status = ?');
    values.push(params.live_status);
  }
  if (params.fidelity) {
    conditions.push('fidelity = ?');
    values.push(params.fidelity);
  }
  if (params.active_only) {
    conditions.push("COALESCE(live_status, '') IN ('live', 'active')");
  }

  const filterWhere = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  const filterValues = [...values];
  const total = (db.prepare(
    `SELECT COUNT(*) as c FROM browsing_sessions ${filterWhere}`
  ).get(...filterValues) as CountResult).c;

  const cursor = decodeTimeCursor(params.cursor);
  if (cursor) {
    conditions.push(`(
      COALESCE(last_item_at, started_at, '') < ?
      OR (COALESCE(last_item_at, started_at, '') = ? AND id < ?)
    )`);
    values.push(cursor.sort_at, cursor.sort_at, cursor.id);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  values.push(limit);
  const data = (db.prepare(
    `SELECT * FROM browsing_sessions
     ${where}
     ORDER BY COALESCE(last_item_at, started_at) DESC, id DESC
     LIMIT ?`
  ).all(...values) as BrowsingSessionDbRow[]).map(mapBrowsingSessionRow);

  let nextCursor: string | undefined;
  if (data.length === limit && data.length > 0) {
    const last = data[data.length - 1];
    const sortAt = last.last_item_at ?? last.started_at;
    if (sortAt) {
      nextCursor = encodeTimeCursor({ sort_at: sortAt, id: last.id });
    }
  }

  return { data, total, cursor: nextCursor };
}

export function getLiveSession(id: string): LiveSessionRow | undefined {
  const db = getDb();
  const row = db.prepare('SELECT * FROM browsing_sessions WHERE id = ?').get(id) as BrowsingSessionDbRow | undefined;
  return row ? mapBrowsingSessionRow(row) : undefined;
}

export function getSessionTurns(sessionId: string): LiveTurnRow[] {
  const db = getDb();
  return db.prepare(
    'SELECT * FROM session_turns WHERE session_id = ? ORDER BY COALESCE(started_at, created_at), id'
  ).all(sessionId) as LiveTurnRow[];
}

interface LiveItemsResult {
  data: LiveItemRow[];
  total: number;
  cursor?: string;
}

export function getSessionItems(sessionId: string, params: LiveItemsListParams = {}): LiveItemsResult {
  const db = getDb();
  const limit = Math.min(Math.max(params.limit ?? 200, 1), 500);
  const conditions = ['session_id = ?'];
  const values: unknown[] = [sessionId];

  if (params.kinds && params.kinds.length > 0) {
    conditions.push(`kind IN (${params.kinds.map(() => '?').join(', ')})`);
    values.push(...params.kinds);
  }
  if (params.cursor) {
    conditions.push('id > ?');
    values.push(Number(params.cursor));
  }

  const where = `WHERE ${conditions.join(' AND ')}`;
  const total = (db.prepare(
    `SELECT COUNT(*) as c FROM session_items WHERE session_id = ?`
  ).get(sessionId) as CountResult).c;

  values.push(limit);
  const data = db.prepare(
    `SELECT * FROM session_items ${where} ORDER BY id ASC LIMIT ?`
  ).all(...values) as LiveItemRow[];

  let cursor: string | undefined;
  if (data.length === limit && data.length > 0) {
    cursor = String(data[data.length - 1].id);
  }

  return { data, total, cursor };
}

export interface TraceQualitySessionSourceRows {
  browsingSession: BrowsingSessionDbRow | undefined;
  turns: LiveTurnRow[];
  sessionItems: LiveItemRow[];
  messages: MessageRow[];
  toolCalls: ToolCallRow[];
}

export function getTraceQualitySessionSourceRows(sessionId: string): TraceQualitySessionSourceRows {
  const db = getDb();
  const browsingSession = db.prepare(
    'SELECT * FROM browsing_sessions WHERE id = ?'
  ).get(sessionId) as BrowsingSessionDbRow | undefined;
  const turns = db.prepare(
    'SELECT * FROM session_turns WHERE session_id = ? ORDER BY COALESCE(started_at, created_at), id'
  ).all(sessionId) as LiveTurnRow[];
  const sessionItems = db.prepare(
    'SELECT * FROM session_items WHERE session_id = ? ORDER BY ordinal, id'
  ).all(sessionId) as LiveItemRow[];
  const messages = db.prepare(
    'SELECT * FROM messages WHERE session_id = ? ORDER BY ordinal, id'
  ).all(sessionId) as MessageRow[];
  const toolCalls = db.prepare(
    'SELECT * FROM tool_calls WHERE session_id = ? ORDER BY id'
  ).all(sessionId) as ToolCallRow[];

  return {
    browsingSession,
    turns,
    sessionItems,
    messages,
    toolCalls,
  };
}

// --- Messages ---

interface MessagesResult {
  data: MessageRow[];
  total: number;
}

export function getSessionMessages(sessionId: string, params: MessagesListParams = {}): MessagesResult {
  const db = getDb();
  const limit = Math.min(Math.max(params.limit ?? 100, 1), 1000);

  const total = (db.prepare(
    'SELECT COUNT(*) as c FROM messages WHERE session_id = ?'
  ).get(sessionId) as CountResult).c;

  if (params.around_ordinal != null) {
    const beforeCount = Math.floor((limit - 1) / 2);
    const maxStartOrdinal = Math.max(0, total - limit);
    const requestedStartOrdinal = Math.max(0, params.around_ordinal - beforeCount);
    const startOrdinal = Math.min(requestedStartOrdinal, maxStartOrdinal);
    const data = db.prepare(
      'SELECT * FROM messages WHERE session_id = ? AND ordinal >= ? ORDER BY ordinal LIMIT ?'
    ).all(sessionId, startOrdinal, limit) as MessageRow[];

    return { data, total };
  }

  const offset = Math.max(params.offset ?? 0, 0);
  const data = db.prepare(
    'SELECT * FROM messages WHERE session_id = ? ORDER BY ordinal LIMIT ? OFFSET ?'
  ).all(sessionId, limit, offset) as MessageRow[];

  return { data, total };
}

export function getSessionActivity(sessionId: string): SessionActivity {
  const db = getDb();
  const summary = db.prepare(`
    SELECT
      COUNT(*) as total_messages,
      COUNT(timestamp) as timestamped_messages,
      MIN(timestamp) as first_timestamp,
      MAX(timestamp) as last_timestamp
    FROM messages
    WHERE session_id = ?
  `).get(sessionId) as {
    total_messages: number;
    timestamped_messages: number;
    first_timestamp: string | null;
    last_timestamp: string | null;
  };

  if (summary.total_messages === 0) {
    return {
      bucket_count: 0,
      total_messages: 0,
      first_timestamp: null,
      last_timestamp: null,
      timestamped_messages: 0,
      untimestamped_messages: 0,
      navigation_basis: 'ordinal',
      data: [],
    };
  }

  const bucketCount = Math.min(40, Math.max(8, summary.total_messages));
  const rows = db.prepare(`
    WITH ordered AS (
      SELECT
        ordinal,
        role,
        timestamp,
        ROW_NUMBER() OVER (ORDER BY ordinal) - 1 as seq,
        COUNT(*) OVER () as total_count
      FROM messages
      WHERE session_id = ?
    ),
    bucketed AS (
      SELECT
        MIN(CAST((seq * ?) / total_count AS INTEGER), ? - 1) as bucket_index,
        ordinal,
        role,
        timestamp
      FROM ordered
    )
    SELECT
      bucket_index,
      MIN(ordinal) as start_ordinal,
      MAX(ordinal) as end_ordinal,
      COUNT(*) as message_count,
      COALESCE(SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END), 0) as user_message_count,
      COALESCE(SUM(CASE WHEN role != 'user' THEN 1 ELSE 0 END), 0) as assistant_message_count,
      MIN(timestamp) as first_timestamp,
      MAX(timestamp) as last_timestamp
    FROM bucketed
    GROUP BY bucket_index
    ORDER BY bucket_index
  `).all(sessionId, bucketCount, bucketCount) as SessionActivityBucket[];

  const rowByIndex = new Map(rows.map(row => [row.bucket_index, row]));
  const data: SessionActivityBucket[] = [];
  for (let bucketIndex = 0; bucketIndex < bucketCount; bucketIndex++) {
    data.push(rowByIndex.get(bucketIndex) ?? {
      bucket_index: bucketIndex,
      start_ordinal: null,
      end_ordinal: null,
      message_count: 0,
      user_message_count: 0,
      assistant_message_count: 0,
      first_timestamp: null,
      last_timestamp: null,
    });
  }

  const untimestampedMessages = Math.max(0, summary.total_messages - summary.timestamped_messages);
  const navigationBasis = summary.timestamped_messages === 0
    ? 'ordinal'
    : untimestampedMessages === 0
      ? 'timestamp'
      : 'mixed';

  return {
    bucket_count: bucketCount,
    total_messages: summary.total_messages,
    first_timestamp: summary.first_timestamp,
    last_timestamp: summary.last_timestamp,
    timestamped_messages: summary.timestamped_messages,
    untimestamped_messages: untimestampedMessages,
    navigation_basis: navigationBasis,
    data,
  };
}

interface PinnedMessageRecord extends PinnedMessageRow {
  message_ordinal: number;
}

interface PinMessageLookup {
  id: number;
  ordinal: number;
}

export function listPinnedMessages(params: PinsListParams & { session_id?: string } = {}): PinnedMessageRow[] {
  const db = getDb();
  const conditions: string[] = [];
  const values: unknown[] = [];

  if (params.session_id) {
    conditions.push('p.session_id = ?');
    values.push(params.session_id);
  } else if (params.project) {
    conditions.push('bs.project = ?');
    values.push(params.project);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  return db.prepare(`
    SELECT
      p.id,
      p.session_id,
      COALESCE(m.id, p.message_id) as message_id,
      p.message_ordinal,
      m.role,
      m.content,
      m.timestamp as message_timestamp,
      p.created_at,
      bs.project as session_project,
      bs.agent as session_agent,
      bs.first_message as session_first_message
    FROM pinned_messages p
    LEFT JOIN messages m
      ON m.session_id = p.session_id
     AND m.ordinal = p.message_ordinal
    LEFT JOIN browsing_sessions bs
      ON bs.id = p.session_id
    ${where}
    ORDER BY p.created_at DESC, p.id DESC
  `).all(...values) as PinnedMessageRecord[];
}

function getPinMessageLookup(sessionId: string, messageId: number): PinMessageLookup | undefined {
  const db = getDb();
  return db.prepare(`
    SELECT id, ordinal
    FROM messages
    WHERE session_id = ? AND id = ?
  `).get(sessionId, messageId) as PinMessageLookup | undefined;
}

export function pinMessage(sessionId: string, messageId: number): PinnedMessageRow | undefined {
  const db = getDb();
  const message = getPinMessageLookup(sessionId, messageId);
  if (!message) return undefined;

  db.prepare(`
    INSERT INTO pinned_messages (session_id, message_id, message_ordinal)
    VALUES (?, ?, ?)
    ON CONFLICT(session_id, message_ordinal)
    DO UPDATE SET message_id = excluded.message_id
  `).run(sessionId, message.id, message.ordinal);

  return db.prepare(`
    SELECT
      p.id,
      p.session_id,
      m.id as message_id,
      p.message_ordinal,
      m.role,
      m.content,
      m.timestamp as message_timestamp,
      p.created_at,
      bs.project as session_project,
      bs.agent as session_agent,
      bs.first_message as session_first_message
    FROM pinned_messages p
    LEFT JOIN messages m
      ON m.session_id = p.session_id
     AND m.ordinal = p.message_ordinal
    LEFT JOIN browsing_sessions bs
      ON bs.id = p.session_id
    WHERE p.session_id = ? AND p.message_ordinal = ?
  `).get(sessionId, message.ordinal) as PinnedMessageRecord | undefined;
}

export function unpinMessage(sessionId: string, messageId: number): { removed: boolean; message_ordinal: number | null } {
  const db = getDb();
  const message = getPinMessageLookup(sessionId, messageId);

  if (message) {
    const result = db.prepare(`
      DELETE FROM pinned_messages
      WHERE session_id = ? AND message_ordinal = ?
    `).run(sessionId, message.ordinal);
    return { removed: result.changes > 0, message_ordinal: message.ordinal };
  }

  const storedPin = db.prepare(`
    SELECT message_ordinal
    FROM pinned_messages
    WHERE session_id = ? AND message_id = ?
  `).get(sessionId, messageId) as { message_ordinal: number } | undefined;

  const result = db.prepare(`
    DELETE FROM pinned_messages
    WHERE session_id = ? AND message_id = ?
  `).run(sessionId, messageId);
  return { removed: result.changes > 0, message_ordinal: storedPin?.message_ordinal ?? null };
}

// --- Search ---

interface FtsSearchResult {
  data: SearchResultRow[];
  total: number;
  cursor?: string;
}

type FtsSearchBaseRow = FtsSearchResult['data'][number];

type FtsSearchRow = FtsSearchBaseRow & {
  search_rank?: number;
};

interface RelevanceCursor {
  rank: number;
  message_id: number;
}

function decodeRelevanceCursor(cursor: string | undefined): RelevanceCursor | null {
  if (!cursor) return null;

  try {
    const parsed = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf-8')) as Partial<RelevanceCursor>;
    if (typeof parsed.rank === 'number' && typeof parsed.message_id === 'number') {
      return {
        rank: parsed.rank,
        message_id: parsed.message_id,
      };
    }
  } catch {
    return null;
  }

  return null;
}

function encodeRelevanceCursor(cursor: RelevanceCursor): string {
  return Buffer.from(JSON.stringify(cursor), 'utf-8').toString('base64url');
}

export function searchMessages(params: SearchParams): FtsSearchResult {
  const db = getDb();
  const limit = Math.min(Math.max(params.limit ?? 20, 1), 100);
  const sort = params.sort === 'relevance' ? 'relevance' : 'recent';

  const conditions: string[] = [];
  const values: unknown[] = [];

  if (params.project) {
    conditions.push('bs.project = ?');
    values.push(params.project);
  }
  if (params.agent) {
    conditions.push('bs.agent = ?');
    values.push(params.agent);
  }

  const joinFilter = conditions.length > 0 ? `AND ${conditions.join(' AND ')}` : '';

  // Count total matches
  const countSql = `
    SELECT COUNT(*) as c
    FROM messages_fts
    JOIN messages m ON m.rowid = messages_fts.rowid
    JOIN browsing_sessions bs ON bs.id = m.session_id
    WHERE messages_fts MATCH ? ${joinFilter}
  `;
  const total = (db.prepare(countSql).get(params.q, ...values) as CountResult).c;

  if (sort === 'relevance') {
    const cursorState = decodeRelevanceCursor(params.cursor);
    const offsetCondition = cursorState
      ? `AND (
          bm25(messages_fts) > ?
          OR (bm25(messages_fts) = ? AND m.id < ?)
        )`
      : '';
    const offsetValues = cursorState
      ? [cursorState.rank, cursorState.rank, cursorState.message_id]
      : [];

    const searchSql = `
      SELECT
        m.session_id,
        m.id as message_id,
        m.ordinal as message_ordinal,
        m.role as message_role,
        snippet(messages_fts, 0, '<mark>', '</mark>', '...', 20) as snippet,
        bs.project as session_project,
        bs.agent as session_agent,
        bs.started_at as session_started_at,
        bs.ended_at as session_ended_at,
        bs.first_message as session_first_message,
        bm25(messages_fts) as search_rank
      FROM messages_fts
      JOIN messages m ON m.rowid = messages_fts.rowid
      JOIN browsing_sessions bs ON bs.id = m.session_id
      WHERE messages_fts MATCH ? ${joinFilter} ${offsetCondition}
      ORDER BY search_rank ASC, m.id DESC
      LIMIT ?
    `;

    const rows = db.prepare(searchSql).all(
      params.q, ...values, ...offsetValues, limit,
    ) as FtsSearchRow[];

    let cursor: string | undefined;
    if (rows.length === limit && rows.length > 0) {
      const last = rows[rows.length - 1];
      if (typeof last?.search_rank === 'number') {
        cursor = encodeRelevanceCursor({
          rank: last.search_rank,
          message_id: last.message_id,
        });
      }
    }

    return {
      data: rows.map(({ search_rank: _searchRank, ...row }) => row as FtsSearchBaseRow),
      total,
      cursor,
    };
  }

  const recentCursor = params.cursor ? parseInt(params.cursor, 10) : null;
  const offsetCondition = Number.isFinite(recentCursor) ? `AND m.id < ?` : '';
  const offsetValues = Number.isFinite(recentCursor) ? [recentCursor] : [];

  const searchSql = `
    SELECT
      m.session_id,
      m.id as message_id,
      m.ordinal as message_ordinal,
      m.role as message_role,
      snippet(messages_fts, 0, '<mark>', '</mark>', '...', 20) as snippet,
      bs.project as session_project,
      bs.agent as session_agent,
      bs.started_at as session_started_at,
      bs.ended_at as session_ended_at,
      bs.first_message as session_first_message
    FROM messages_fts
    JOIN messages m ON m.rowid = messages_fts.rowid
    JOIN browsing_sessions bs ON bs.id = m.session_id
    WHERE messages_fts MATCH ? ${joinFilter} ${offsetCondition}
    ORDER BY m.id DESC
    LIMIT ?
  `;

  const data = db.prepare(searchSql).all(
    params.q, ...values, ...offsetValues, limit,
  ) as FtsSearchResult['data'];

  let cursor: string | undefined;
  if (data.length === limit && data.length > 0) {
    cursor = String(data[data.length - 1].message_id);
  }

  return { data, total, cursor };
}

// --- Analytics ---
type AnalyticsCoverageScope = AnalyticsCoverage['metric_scope'];

interface AnalyticsFilterState {
  conditions: string[];
  values: unknown[];
  where: string;
}

function qualifyColumn(alias: string | undefined, column: string): string {
  return alias ? `${alias}.${column}` : column;
}

function buildAnalyticsFilterState(params: AnalyticsParams = {}, alias?: string): AnalyticsFilterState {
  const conditions: string[] = [];
  const values: unknown[] = [];

  if (params.project) {
    conditions.push(`${qualifyColumn(alias, 'project')} = ?`);
    values.push(params.project);
  }
  if (params.agent) {
    conditions.push(`${qualifyColumn(alias, 'agent')} = ?`);
    values.push(params.agent);
  }
  if (params.date_from) {
    conditions.push(`${qualifyColumn(alias, 'started_at')} >= ?`);
    values.push(params.date_from);
  }
  if (params.date_to) {
    conditions.push(`${qualifyColumn(alias, 'started_at')} < date(?, '+1 day')`);
    values.push(params.date_to);
  }

  return {
    conditions,
    values,
    where: conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '',
  };
}

function analyticsFidelityExpr(alias?: string): string {
  const fidelityColumn = qualifyColumn(alias, 'fidelity');
  const integrationModeColumn = qualifyColumn(alias, 'integration_mode');
  return `CASE
    WHEN ${fidelityColumn} = 'full' THEN 'full'
    WHEN ${fidelityColumn} = 'summary' THEN 'summary'
    WHEN ${integrationModeColumn} = 'claude-jsonl' THEN 'full'
    ELSE 'unknown'
  END`;
}

function analyticsCapabilityExpr(
  capability: 'history' | 'search' | 'tool_analytics' | 'live_items',
  alias?: string,
): string {
  const capabilitiesColumn = qualifyColumn(alias, 'capabilities_json');
  const fidelityColumn = qualifyColumn(alias, 'fidelity');
  const integrationModeColumn = qualifyColumn(alias, 'integration_mode');
  return `COALESCE(
    json_extract(${capabilitiesColumn}, '$.${capability}'),
    CASE
      WHEN ${integrationModeColumn} = 'claude-jsonl' OR ${fidelityColumn} = 'full' THEN 'full'
      WHEN ${fidelityColumn} = 'summary' THEN 'none'
      ELSE 'unknown'
    END
  )`;
}

function toolAnalyticsCapableCondition(alias?: string): string {
  return `${analyticsCapabilityExpr('tool_analytics', alias)} IN ('summary', 'full')`;
}

function roundMetric(value: number): number {
  return Math.round(value * 100) / 100;
}

function inclusiveDateSpanDays(earliest: string | null, latest: string | null): number {
  if (!earliest || !latest) return 0;
  const earliestDate = new Date(`${earliest.slice(0, 10)}T00:00:00.000Z`);
  const latestDate = new Date(`${latest.slice(0, 10)}T00:00:00.000Z`);
  return Math.max(1, Math.round(
    (latestDate.getTime() - earliestDate.getTime()) / 86_400_000
  ) + 1);
}

function enumerateDateRange(from: string, to: string): string[] {
  const start = new Date(`${from}T00:00:00.000Z`);
  const end = new Date(`${to}T00:00:00.000Z`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || start > end) {
    return [];
  }

  const dates: string[] = [];
  for (let cursor = start.getTime(); cursor <= end.getTime(); cursor += 86_400_000) {
    dates.push(new Date(cursor).toISOString().slice(0, 10));
  }
  return dates;
}

function addDaysToDateString(date: string, days: number): string | null {
  const parsed = new Date(`${date}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  parsed.setUTCDate(parsed.getUTCDate() + days);
  return parsed.toISOString().slice(0, 10);
}

function parseJsonString(value: string | null | undefined): unknown {
  if (!value) return undefined;
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}

interface SkillAccumulator {
  total: number;
  skills: Map<string, number>;
}

function addSkillCount(days: Map<string, SkillAccumulator>, date: string, skillName: string): void {
  const existing = days.get(date) ?? { total: 0, skills: new Map<string, number>() };
  existing.total += 1;
  existing.skills.set(skillName, (existing.skills.get(skillName) ?? 0) + 1);
  days.set(date, existing);
}

export function getAnalyticsCoverage(
  params: AnalyticsParams = {},
  scope: AnalyticsCoverageScope = 'all_sessions',
): AnalyticsCoverage {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params);
  const includedCondition = scope === 'tool_analytics_capable' ? toolAnalyticsCapableCondition() : '1 = 1';
  const fidelityExpr = analyticsFidelityExpr();
  const historyExpr = analyticsCapabilityExpr('history');
  const searchExpr = analyticsCapabilityExpr('search');
  const toolAnalyticsExpr = analyticsCapabilityExpr('tool_analytics');
  const liveItemsExpr = analyticsCapabilityExpr('live_items');

  const row = db.prepare(`
    SELECT
      COUNT(*) as matching_sessions,
      COALESCE(SUM(CASE WHEN ${includedCondition} THEN 1 ELSE 0 END), 0) as included_sessions,
      COALESCE(SUM(CASE WHEN ${fidelityExpr} = 'full' THEN 1 ELSE 0 END), 0) as fidelity_full,
      COALESCE(SUM(CASE WHEN ${fidelityExpr} = 'summary' THEN 1 ELSE 0 END), 0) as fidelity_summary,
      COALESCE(SUM(CASE WHEN ${fidelityExpr} = 'unknown' THEN 1 ELSE 0 END), 0) as fidelity_unknown,
      COALESCE(SUM(CASE WHEN ${historyExpr} = 'full' THEN 1 ELSE 0 END), 0) as history_full,
      COALESCE(SUM(CASE WHEN ${historyExpr} = 'summary' THEN 1 ELSE 0 END), 0) as history_summary,
      COALESCE(SUM(CASE WHEN ${historyExpr} = 'none' THEN 1 ELSE 0 END), 0) as history_none,
      COALESCE(SUM(CASE WHEN ${historyExpr} = 'unknown' THEN 1 ELSE 0 END), 0) as history_unknown,
      COALESCE(SUM(CASE WHEN ${searchExpr} = 'full' THEN 1 ELSE 0 END), 0) as search_full,
      COALESCE(SUM(CASE WHEN ${searchExpr} = 'summary' THEN 1 ELSE 0 END), 0) as search_summary,
      COALESCE(SUM(CASE WHEN ${searchExpr} = 'none' THEN 1 ELSE 0 END), 0) as search_none,
      COALESCE(SUM(CASE WHEN ${searchExpr} = 'unknown' THEN 1 ELSE 0 END), 0) as search_unknown,
      COALESCE(SUM(CASE WHEN ${toolAnalyticsExpr} = 'full' THEN 1 ELSE 0 END), 0) as tool_analytics_full,
      COALESCE(SUM(CASE WHEN ${toolAnalyticsExpr} = 'summary' THEN 1 ELSE 0 END), 0) as tool_analytics_summary,
      COALESCE(SUM(CASE WHEN ${toolAnalyticsExpr} = 'none' THEN 1 ELSE 0 END), 0) as tool_analytics_none,
      COALESCE(SUM(CASE WHEN ${toolAnalyticsExpr} = 'unknown' THEN 1 ELSE 0 END), 0) as tool_analytics_unknown,
      COALESCE(SUM(CASE WHEN ${liveItemsExpr} = 'full' THEN 1 ELSE 0 END), 0) as live_items_full,
      COALESCE(SUM(CASE WHEN ${liveItemsExpr} = 'summary' THEN 1 ELSE 0 END), 0) as live_items_summary,
      COALESCE(SUM(CASE WHEN ${liveItemsExpr} = 'none' THEN 1 ELSE 0 END), 0) as live_items_none,
      COALESCE(SUM(CASE WHEN ${liveItemsExpr} = 'unknown' THEN 1 ELSE 0 END), 0) as live_items_unknown
    FROM browsing_sessions
    ${filter.where}
  `).get(...filter.values) as Record<string, number>;

  const matchingSessions = row['matching_sessions'] ?? 0;
  const includedSessions = row['included_sessions'] ?? 0;

  return {
    metric_scope: scope,
    matching_sessions: matchingSessions,
    included_sessions: includedSessions,
    excluded_sessions: Math.max(0, matchingSessions - includedSessions),
    fidelity_breakdown: {
      full: row['fidelity_full'] ?? 0,
      summary: row['fidelity_summary'] ?? 0,
      unknown: row['fidelity_unknown'] ?? 0,
    },
    capability_breakdown: {
      history: {
        full: row['history_full'] ?? 0,
        summary: row['history_summary'] ?? 0,
        none: row['history_none'] ?? 0,
        unknown: row['history_unknown'] ?? 0,
      },
      search: {
        full: row['search_full'] ?? 0,
        summary: row['search_summary'] ?? 0,
        none: row['search_none'] ?? 0,
        unknown: row['search_unknown'] ?? 0,
      },
      tool_analytics: {
        full: row['tool_analytics_full'] ?? 0,
        summary: row['tool_analytics_summary'] ?? 0,
        none: row['tool_analytics_none'] ?? 0,
        unknown: row['tool_analytics_unknown'] ?? 0,
      },
      live_items: {
        full: row['live_items_full'] ?? 0,
        summary: row['live_items_summary'] ?? 0,
        none: row['live_items_none'] ?? 0,
        unknown: row['live_items_unknown'] ?? 0,
      },
    },
    note: scope === 'tool_analytics_capable'
      ? 'Only sessions whose capability contract exposes tool analytics are included in this metric.'
      : 'This metric includes every session matching the current filters, including summary-only sessions.',
  };
}

export function getAnalyticsSummary(params: AnalyticsParams = {}): AnalyticsSummary {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params);

  const row = db.prepare(`
    SELECT
      COUNT(*) as total_sessions,
      COALESCE(SUM(message_count), 0) as total_messages,
      COALESCE(SUM(user_message_count), 0) as total_user_messages,
      MIN(started_at) as earliest,
      MAX(started_at) as latest
    FROM browsing_sessions
    ${filter.where}
  `).get(...filter.values) as {
    total_sessions: number;
    total_messages: number;
    total_user_messages: number;
    earliest: string | null;
    latest: string | null;
  };

  let dailyAvgSessions = 0;
  let dailyAvgMessages = 0;
  if (row.earliest && row.latest) {
    const days = inclusiveDateSpanDays(row.earliest, row.latest);
    dailyAvgSessions = roundMetric(row.total_sessions / days);
    dailyAvgMessages = roundMetric(row.total_messages / days);
  }

  return {
    total_sessions: row.total_sessions,
    total_messages: row.total_messages,
    total_user_messages: row.total_user_messages,
    daily_average_sessions: dailyAvgSessions,
    daily_average_messages: dailyAvgMessages,
    date_range: {
      earliest: row.earliest,
      latest: row.latest,
    },
    coverage: getAnalyticsCoverage(params, 'all_sessions'),
  };
}

export function getAnalyticsActivity(params: AnalyticsParams = {}): ActivityDataPoint[] {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params);

  return db.prepare(`
    SELECT
      date(started_at) as date,
      COUNT(*) as sessions,
      COALESCE(SUM(message_count), 0) as messages,
      COALESCE(SUM(user_message_count), 0) as user_messages
    FROM browsing_sessions
    ${filter.where}
    GROUP BY date(started_at)
    ORDER BY date
  `).all(...filter.values) as ActivityDataPoint[];
}

export function getAnalyticsProjects(params: AnalyticsParams = {}): ProjectBreakdown[] {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params);
  const where = ['project IS NOT NULL', ...filter.conditions].join(' AND ');

  return db.prepare(`
    SELECT
      project,
      COUNT(*) as session_count,
      COALESCE(SUM(message_count), 0) as message_count,
      COALESCE(SUM(user_message_count), 0) as user_message_count
    FROM browsing_sessions
    WHERE ${where}
    GROUP BY project
    ORDER BY message_count DESC, session_count DESC, project ASC
  `).all(...filter.values) as ProjectBreakdown[];
}

export function getAnalyticsTools(params: AnalyticsParams = {}): ToolUsageStat[] {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params, 'bs');
  const where = [...filter.conditions, toolAnalyticsCapableCondition('bs')].join(' AND ');

  return db.prepare(`
    SELECT
      tc.tool_name,
      tc.category,
      COUNT(*) as count
    FROM tool_calls tc
    JOIN browsing_sessions bs ON bs.id = tc.session_id
    WHERE ${where}
    GROUP BY tc.tool_name, tc.category
    ORDER BY count DESC, tc.tool_name ASC
  `).all(...filter.values) as ToolUsageStat[];
}

export function getMonitorToolStats(params: UsageParams = {}): MonitorToolStat[] {
  const db = getDb();
  const filter = buildUsageFilterState(params, 'e');
  const where = [...filter.conditions, 'e.tool_name IS NOT NULL'].join(' AND ');

  const rows = db.prepare(`
    SELECT
      e.tool_name,
      COUNT(*) as total_calls,
      COALESCE(SUM(CASE WHEN e.status = 'error' THEN 1 ELSE 0 END), 0) as error_count,
      ROUND(CAST(COALESCE(SUM(CASE WHEN e.status = 'error' THEN 1 ELSE 0 END), 0) AS REAL) / COUNT(*), 4) as error_rate,
      ROUND(AVG(e.duration_ms)) as avg_duration_ms
    FROM events e
    WHERE ${where}
    GROUP BY e.tool_name
    ORDER BY total_calls DESC, e.tool_name ASC
  `).all(...filter.values) as Array<Omit<MonitorToolStat, 'by_agent'>>;

  const agentRows = db.prepare(`
    SELECT
      e.tool_name,
      e.agent_type,
      COUNT(*) as count
    FROM events e
    WHERE ${where}
    GROUP BY e.tool_name, e.agent_type
    ORDER BY e.tool_name, count DESC
  `).all(...filter.values) as Array<{ tool_name: string; agent_type: string; count: number }>;

  const byAgent = new Map<string, Record<string, number>>();
  for (const row of agentRows) {
    const next = byAgent.get(row.tool_name) ?? {};
    next[row.agent_type] = row.count;
    byAgent.set(row.tool_name, next);
  }

  return rows.map(row => ({
    ...row,
    by_agent: byAgent.get(row.tool_name) ?? {},
  }));
}

function updateMonitorSessionStatuses(timeoutMinutes: number): void {
  const db = getDb();
  db.prepare(`
    UPDATE sessions SET status = 'idle'
    WHERE status = 'active'
    AND last_event_at < datetime('now', ? || ' minutes')
  `).run(`-${timeoutMinutes}`);

  db.prepare(`
    UPDATE sessions SET status = 'ended', ended_at = datetime('now')
    WHERE status = 'idle'
    AND last_event_at < datetime('now', ? || ' minutes')
  `).run(`-${timeoutMinutes * 2}`);
}

export function listMonitorSessions(params: MonitorSessionsParams = {}): { sessions: MonitorSessionRow[]; total: number } {
  const db = getDb();
  updateMonitorSessionStatuses(config.sessionTimeoutMinutes);

  const conditions: string[] = [];
  const values: unknown[] = [];

  if (params.status) {
    conditions.push('s.status = ?');
    values.push(params.status);
  }
  if (params.exclude_status) {
    conditions.push('s.status != ?');
    values.push(params.exclude_status);
  }
  if (params.project) {
    conditions.push('s.project = ?');
    values.push(params.project);
  }
  if (params.agent) {
    conditions.push('s.agent_type = ?');
    values.push(params.agent);
  }
  if (params.date_from) {
    conditions.push('datetime(s.last_event_at) >= datetime(?)');
    values.push(params.date_from);
  }
  if (params.date_to) {
    conditions.push(`datetime(s.last_event_at) < datetime(?, '+1 day')`);
    values.push(params.date_to);
  }

  const requestedLimit = Number.isFinite(params.limit) ? Math.trunc(params.limit as number) : 50;
  const applyLimit = requestedLimit > 0;
  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  const queryValues = applyLimit ? [...values, requestedLimit] : values;

  // Page the sessions first, then compute all seven event aggregates in a single
  // grouped pass restricted to that page, instead of running seven correlated
  // subqueries per returned row. Byte-identical to the previous correlated query
  // (see tests/monitor-session-list.test.ts, which diffs both against seeded
  // edge cases); the SUM(session_id IN page) restriction keeps it index-backed
  // by idx_events_session_cost rather than scanning all events.
  const sessions = db.prepare(`
    WITH page AS (
      SELECT s.*
      FROM sessions s
      ${where}
      ORDER BY
        CASE s.status WHEN 'active' THEN 0 WHEN 'idle' THEN 1 ELSE 2 END,
        datetime(s.last_event_at) DESC,
        s.id DESC
      ${applyLimit ? 'LIMIT ?' : ''}
    ),
    agg AS (
      SELECT
        e.session_id,
        COUNT(*) AS event_count,
        SUM(e.tokens_in) AS tokens_in,
        SUM(e.tokens_out) AS tokens_out,
        SUM(e.cost_usd) AS total_cost_usd,
        COUNT(DISTINCT CASE
          WHEN json_valid(e.metadata) = 1
            AND e.tool_name IN ('Edit', 'Write', 'MultiEdit', 'apply_patch', 'write_stdin')
            AND json_extract(e.metadata, '$.file_path') IS NOT NULL
          THEN json_extract(e.metadata, '$.file_path')
        END) AS files_edited,
        SUM(CASE
          WHEN json_valid(e.metadata) = 1 AND json_extract(e.metadata, '$.lines_added') IS NOT NULL
          THEN CAST(json_extract(e.metadata, '$.lines_added') AS INTEGER)
        END) AS lines_added,
        SUM(CASE
          WHEN json_valid(e.metadata) = 1 AND json_extract(e.metadata, '$.lines_removed') IS NOT NULL
          THEN CAST(json_extract(e.metadata, '$.lines_removed') AS INTEGER)
        END) AS lines_removed
      FROM events e
      WHERE e.session_id IN (SELECT id FROM page)
      GROUP BY e.session_id
    )
    SELECT
      page.*,
      json_extract(page.metadata, '$.mode') as mode,
      COALESCE(agg.event_count, 0) as event_count,
      COALESCE(agg.tokens_in, 0) as tokens_in,
      COALESCE(agg.tokens_out, 0) as tokens_out,
      COALESCE(agg.total_cost_usd, 0) as total_cost_usd,
      COALESCE(agg.files_edited, 0) as files_edited,
      COALESCE(agg.lines_added, 0) as lines_added,
      COALESCE(agg.lines_removed, 0) as lines_removed
    FROM page
    LEFT JOIN agg ON agg.session_id = page.id
    ORDER BY
      CASE page.status WHEN 'active' THEN 0 WHEN 'idle' THEN 1 ELSE 2 END,
      datetime(page.last_event_at) DESC,
      page.id DESC
  `).all(...queryValues) as MonitorSessionRow[];

  return { sessions, total: sessions.length };
}

export function listMonitorEvents(params: MonitorEventsParams = {}): { events: MonitorEventRow[]; total: number } {
  const db = getDb();
  const conditions: string[] = [];
  const values: unknown[] = [];

  if (params.agent) {
    conditions.push('agent_type = ?');
    values.push(params.agent);
  }
  if (params.event_type) {
    conditions.push('event_type = ?');
    values.push(params.event_type);
  }
  if (params.tool_name) {
    conditions.push('tool_name = ?');
    values.push(params.tool_name);
  }
  if (params.session_id) {
    conditions.push('session_id = ?');
    values.push(params.session_id);
  }
  if (params.branch) {
    conditions.push('branch = ?');
    values.push(params.branch);
  }
  if (params.model) {
    conditions.push('model = ?');
    values.push(params.model);
  }
  if (params.source) {
    conditions.push('source = ?');
    values.push(params.source);
  }
  if (params.since) {
    conditions.push('created_at >= datetime(?)');
    values.push(params.since);
  }
  if (params.until) {
    conditions.push('created_at <= datetime(?)');
    values.push(params.until);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  const limit = Math.min(Math.max(params.limit ?? 50, 1), 500);
  const offset = Math.max(params.offset ?? 0, 0);
  const total = (db.prepare(`SELECT COUNT(*) as c FROM events ${where}`).get(...values) as CountResult).c;
  const events = db.prepare(`
    SELECT * FROM events ${where}
    ORDER BY datetime(created_at) DESC, id DESC
    LIMIT ? OFFSET ?
  `).all(...values, limit, offset) as MonitorEventRow[];

  return { events, total };
}

const MONITOR_QUOTA_DEFAULTS: Record<'claude' | 'codex', Omit<MonitorQuotaSnapshot, 'provider'>> = {
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

function monitorQuotaWindow(row: {
  used_percent: number | null;
  resets_at: string | null;
  window_minutes: number | null;
}): MonitorQuotaSnapshot['primary'] {
  if (row.used_percent == null || !Number.isFinite(row.used_percent)) return null;
  const usedPercent = Math.max(0, Math.min(row.used_percent, 100));
  return {
    used_percent: usedPercent,
    remaining_percent: Math.max(0, Math.min(100 - usedPercent, 100)),
    resets_at: row.resets_at,
    window_minutes: row.window_minutes,
  };
}

function listMonitorProviderQuotas(): MonitorQuotaSnapshot[] {
  const db = getDb();
  const rows = db.prepare(`
    SELECT
      provider, agent_type, status, source, updated_at, account_label, plan_type,
      limit_id, limit_name, error_message, primary_used_percent, primary_window_minutes,
      primary_resets_at, secondary_used_percent, secondary_window_minutes,
      secondary_resets_at, credits_has_credits, credits_unlimited, credits_balance
    FROM provider_quotas
  `).all() as Array<{
    provider: 'claude' | 'codex';
    agent_type: 'claude_code' | 'codex';
    status: MonitorQuotaSnapshot['status'];
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

  const byProvider = new Map(rows.map(row => [row.provider, row]));
  return (['claude', 'codex'] as const).map((provider) => {
    const row = byProvider.get(provider);
    if (!row) return { provider, ...MONITOR_QUOTA_DEFAULTS[provider] };

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
      primary: monitorQuotaWindow({
        used_percent: row.primary_used_percent,
        resets_at: row.primary_resets_at,
        window_minutes: row.primary_window_minutes,
      }),
      secondary: monitorQuotaWindow({
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

export function getMonitorStats(params: MonitorStatsParams = {}): MonitorStats {
  const db = getDb();
  updateMonitorSessionStatuses(config.sessionTimeoutMinutes);

  // The Monitor is the live-activity view: batch-imported benchmark rows are
  // always excluded from its aggregates (no opt-in), and benchmark sessions are
  // marked ended at insert so they never reach the live session lists/counts.
  const conditions: string[] = [excludeBenchmarkUsageCondition('e')];
  const values: unknown[] = [];

  if (params.agent) {
    conditions.push('e.agent_type = ?');
    values.push(params.agent);
  }
  if (params.since) {
    conditions.push('e.created_at >= datetime(?)');
    values.push(params.since);
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  const totals = db.prepare(`
    SELECT
      COUNT(*) as total_events,
      ${reconciledUsageSum('e', 'tokens_in')} as total_tokens_in,
      ${reconciledUsageSum('e', 'tokens_out')} as total_tokens_out,
      ${reconciledUsageSum('e', 'cost_usd')} as total_cost_usd
    FROM events e ${where}
  `).get(...values) as {
    total_events: number;
    total_tokens_in: number;
    total_tokens_out: number;
    total_cost_usd: number;
  };

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

  const toolWhere = conditions.length > 0 ? `${where} AND tool_name IS NOT NULL` : 'WHERE tool_name IS NOT NULL';
  const toolRows = db.prepare(`
    SELECT tool_name, COUNT(*) as count FROM events e
    ${toolWhere}
    GROUP BY tool_name ORDER BY count DESC
  `).all(...values) as { tool_name: string; count: number }[];
  const toolBreakdown = Object.fromEntries(toolRows.map(row => [row.tool_name, row.count]));

  const agentRows = db.prepare(`
    SELECT agent_type, COUNT(*) as count FROM events e ${where}
    GROUP BY agent_type ORDER BY count DESC
  `).all(...values) as { agent_type: string; count: number }[];
  const agentBreakdown = Object.fromEntries(agentRows.map(row => [row.agent_type, row.count]));

  const modelWhere = conditions.length > 0 ? `${where} AND model IS NOT NULL` : 'WHERE model IS NOT NULL';
  const modelRows = db.prepare(`
    SELECT model, COUNT(*) as count FROM events e
    ${modelWhere}
    GROUP BY model ORDER BY count DESC
  `).all(...values) as { model: string; count: number }[];
  const modelBreakdown = Object.fromEntries(modelRows.map(row => [row.model, row.count]));

  const branchRows = db.prepare(`
    SELECT DISTINCT branch FROM sessions WHERE branch IS NOT NULL ORDER BY last_event_at DESC
  `).all() as { branch: string }[];

  const quotaMonitor = listMonitorProviderQuotas();
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
    branches: branchRows.map(row => row.branch),
    quota_monitor: quotaMonitor,
    usage_monitor: quotaMonitor,
  };
}

export function getMonitorFilterOptions(): MonitorFilterOptions {
  const db = getDb();

  const agentTypes = (db.prepare(
    'SELECT DISTINCT agent_type FROM events WHERE agent_type IS NOT NULL ORDER BY agent_type'
  ).all() as { agent_type: string }[]).map(row => row.agent_type);

  const eventTypes = (db.prepare(
    'SELECT DISTINCT event_type FROM events WHERE event_type IS NOT NULL ORDER BY event_type'
  ).all() as { event_type: string }[]).map(row => row.event_type);

  const toolNames = (db.prepare(
    'SELECT DISTINCT tool_name FROM events WHERE tool_name IS NOT NULL ORDER BY tool_name'
  ).all() as { tool_name: string }[]).map(row => row.tool_name);

  const models = (db.prepare(
    'SELECT DISTINCT model FROM events WHERE model IS NOT NULL ORDER BY model'
  ).all() as { model: string }[]).map(row => row.model);

  const projects = (db.prepare(
    'SELECT DISTINCT project FROM sessions WHERE project IS NOT NULL ORDER BY project'
  ).all() as { project: string }[]).map(row => row.project);

  const branchRows = db.prepare(`
    SELECT branch, project, MAX(last_event_at) as latest
    FROM sessions
    WHERE branch IS NOT NULL AND branch != 'HEAD'
    GROUP BY branch
    ORDER BY latest DESC
  `).all() as { branch: string; project: string | null; latest: string }[];
  const branches = branchRows.map(row => ({
    value: row.branch,
    label: row.project ? `${row.project} / ${row.branch}` : row.branch,
  }));

  const sources = (db.prepare(
    'SELECT DISTINCT source FROM events WHERE source IS NOT NULL ORDER BY source'
  ).all() as { source: string }[]).map(row => row.source);

  return { agent_types: agentTypes, event_types: eventTypes, tool_names: toolNames, models, projects, branches, sources };
}

export function getMonitorSessionWithEvents(sessionId: string, eventLimit = 10): {
  session: MonitorSessionRow | undefined;
  events: MonitorEventRow[];
} {
  const db = getDb();
  updateMonitorSessionStatuses(config.sessionTimeoutMinutes);
  const limit = Math.min(Math.max(eventLimit, 0), 500);

  const session = db.prepare(`
    SELECT s.*,
      json_extract(s.metadata, '$.mode') as mode,
      COALESCE((SELECT COUNT(*) FROM events e WHERE e.session_id = s.id), 0) as event_count,
      COALESCE((SELECT SUM(e.tokens_in) FROM events e WHERE e.session_id = s.id), 0) as tokens_in,
      COALESCE((SELECT SUM(e.tokens_out) FROM events e WHERE e.session_id = s.id), 0) as tokens_out,
      COALESCE((SELECT SUM(e.cost_usd) FROM events e WHERE e.session_id = s.id), 0) as total_cost_usd,
      COALESCE((SELECT COUNT(DISTINCT json_extract(e.metadata, '$.file_path')) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND e.tool_name IN ('Edit', 'Write', 'MultiEdit', 'apply_patch', 'write_stdin') AND json_extract(e.metadata, '$.file_path') IS NOT NULL), 0) as files_edited,
      COALESCE((SELECT SUM(CAST(json_extract(e.metadata, '$.lines_added') AS INTEGER)) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND json_extract(e.metadata, '$.lines_added') IS NOT NULL), 0) as lines_added,
      COALESCE((SELECT SUM(CAST(json_extract(e.metadata, '$.lines_removed') AS INTEGER)) FROM events e WHERE e.session_id = s.id AND json_valid(e.metadata) = 1 AND json_extract(e.metadata, '$.lines_removed') IS NOT NULL), 0) as lines_removed
    FROM sessions s
    WHERE s.id = ?
  `).get(sessionId) as MonitorSessionRow | undefined;

  const events = db.prepare(`
    SELECT * FROM events
    WHERE session_id = ?
    ORDER BY datetime(created_at) DESC, id DESC
    LIMIT ?
  `).all(sessionId, limit) as MonitorEventRow[];

  return { session, events };
}

function monitorTranscriptDetail(event: MonitorTranscriptEvent): string | undefined {
  try {
    const meta = JSON.parse(event.metadata || '{}') as Record<string, unknown>;
    if (typeof meta.message === 'string' && event.event_type === 'user_prompt') return meta.message;
    if (typeof meta.content_preview === 'string') return meta.content_preview;
    if (typeof meta.command === 'string') return meta.command;
    if (typeof meta.file_path === 'string') return meta.file_path;
    if (typeof meta.pattern === 'string') return meta.pattern;
    if (typeof meta.query === 'string') return meta.query;
    if (typeof meta.diff_preview === 'string') return meta.diff_preview;
    if (typeof meta.error === 'string') return meta.error;
    if (meta.error && typeof meta.error === 'object' && 'message' in meta.error) {
      const message = (meta.error as { message?: unknown }).message;
      if (typeof message === 'string') return message;
    }
    if (meta.arguments && typeof meta.arguments === 'object' && 'cmd' in meta.arguments) {
      const command = (meta.arguments as { cmd?: unknown }).cmd;
      if (typeof command === 'string') return command;
    }
  } catch {
    return undefined;
  }
  return undefined;
}

function monitorTranscriptRole(eventType: string): MonitorTranscriptEntry['role'] {
  switch (eventType) {
    case 'session_start':
    case 'session_end':
      return 'system';
    case 'user_prompt':
      return 'user';
    case 'tool_use':
      return 'tool';
    case 'error':
      return 'assistant';
    default:
      return 'assistant';
  }
}

function monitorTranscriptEntry(event: MonitorTranscriptEvent): MonitorTranscriptEntry {
  const entry: MonitorTranscriptEntry = {
    role: monitorTranscriptRole(event.event_type),
    type: event.event_type,
    timestamp: event.client_timestamp || event.created_at,
  };

  if (event.tool_name) entry.tool_name = event.tool_name;
  const detail = monitorTranscriptDetail(event);
  if (detail) entry.detail = detail;
  if (event.status !== 'success') entry.status = event.status;
  if (event.model) entry.model = event.model;
  if (event.tokens_in > 0) entry.tokens_in = event.tokens_in;
  if (event.tokens_out > 0) entry.tokens_out = event.tokens_out;
  if (event.cost_usd && event.cost_usd > 0) entry.cost_usd = event.cost_usd;
  if (event.duration_ms) entry.duration_ms = event.duration_ms;

  return entry;
}

function monitorTranscriptContent(entry: MonitorTranscriptEntry): string {
  const label = entry.tool_name ? `${entry.type} > ${entry.tool_name}` : entry.type;
  return entry.detail ? `${label}: ${entry.detail}` : label;
}

export function getMonitorSessionTranscript(sessionId: string): {
  session_id: string;
  entries: MonitorTranscriptEntry[];
  transcript: MonitorTranscriptRow[];
} | null {
  const db = getDb();
  const events = db.prepare(`
    SELECT id, event_type, tool_name, status, tokens_in, tokens_out,
           model, cost_usd, duration_ms, created_at, client_timestamp, metadata
    FROM events
    WHERE session_id = ?
    ORDER BY datetime(created_at) ASC, id ASC
  `).all(sessionId) as MonitorTranscriptEvent[];

  if (events.length === 0) return null;

  const entries = events.map(monitorTranscriptEntry);
  return {
    session_id: sessionId,
    entries,
    transcript: entries.map(entry => ({
      role: entry.role,
      content: monitorTranscriptContent(entry),
      timestamp: entry.timestamp,
    })),
  };
}

export function getAnalyticsSkillsDaily(params: AnalyticsParams = {}): SkillUsageDay[] {
  const days = new Map<string, SkillAccumulator>();

  for (const occurrence of selectSkillInvocationOccurrences(getDb(), params)) {
    addSkillCount(days, occurrence.timestamp.slice(0, 10), occurrence.skillName);
  }

  return [...days.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([date, info]) => ({
      date,
      total: info.total,
      skills: [...info.skills.entries()]
        .map(([skill_name, count]) => ({ skill_name, count }))
        .sort((left, right) => right.count - left.count || left.skill_name.localeCompare(right.skill_name)),
    }));
}

const INTERRUPT_MARKER = '[Request interrupted by user';

type UserMessageKind = 'interrupt' | 'prompt';

/**
 * Classify a stored user-message content payload for misfire detection. Only
 * `text` blocks count: an interrupt is a text block starting with the interrupt
 * marker; any other non-empty text block is a genuine prompt. Tool-result-only
 * messages (the mechanical returns that also arrive in user-role messages) are
 * not turn boundaries and resolve to null. This deliberately ignores the marker
 * when it appears inside a quoted tool_result rather than as the user's own text.
 */
function classifyUserMessage(contentJson: string | null): UserMessageKind | null {
  const parsed = parseJsonString(contentJson);

  const texts: string[] = [];
  if (typeof parsed === 'string') {
    texts.push(parsed);
  } else if (Array.isArray(parsed)) {
    for (const block of parsed) {
      if (block && typeof block === 'object' && (block as { type?: unknown }).type === 'text') {
        const text = (block as { text?: unknown }).text;
        if (typeof text === 'string') texts.push(text);
      }
    }
  }

  let hasPrompt = false;
  for (const text of texts) {
    const trimmed = text.trimStart();
    if (trimmed.startsWith(INTERRUPT_MARKER)) return 'interrupt';
    if (trimmed.length > 0) hasPrompt = true;
  }
  return hasPrompt ? 'prompt' : null;
}

interface UserTurnBoundary {
  ordinal: number;
  kind: UserMessageKind;
}

/**
 * For each session, the ordinal-sorted list of genuine user turns (interrupts
 * and prompts). Pre-filtered in SQL to user messages that carry a text block,
 * which drops the tool-result-only messages that otherwise dominate the payload.
 */
function loadUserTurnBoundaries(sessionIds: string[]): Map<string, UserTurnBoundary[]> {
  const boundaries = new Map<string, UserTurnBoundary[]>();
  if (sessionIds.length === 0) return boundaries;

  const db = getDb();
  const CHUNK = 400;
  for (let i = 0; i < sessionIds.length; i += CHUNK) {
    const chunk = sessionIds.slice(i, i + CHUNK);
    const placeholders = chunk.map(() => '?').join(', ');
    const rows = db.prepare(`
      SELECT session_id, ordinal, content
      FROM messages
      WHERE role = 'user'
        AND session_id IN (${placeholders})
        AND content LIKE '%"type":"text"%'
      ORDER BY session_id, ordinal
    `).all(...chunk) as Array<{ session_id: string; ordinal: number; content: string | null }>;

    for (const row of rows) {
      const kind = classifyUserMessage(row.content);
      if (!kind) continue;
      const list = boundaries.get(row.session_id) ?? [];
      list.push({ ordinal: row.ordinal, kind });
      boundaries.set(row.session_id, list);
    }
  }
  return boundaries;
}

/** True when the invoking turn was interrupted before the next genuine prompt. */
function invocationMisfired(boundaries: UserTurnBoundary[] | undefined, invocationOrdinal: number): boolean {
  if (!boundaries) return false;
  for (const boundary of boundaries) {
    if (boundary.ordinal <= invocationOrdinal) continue;
    return boundary.kind === 'interrupt';
  }
  return false;
}

const CATALOG_REFRESH_TTL_MS = 60_000;
let lastCatalogRefreshMs = 0;
let lastCatalogScan: CatalogSkill[] | null = null;

/**
 * Stamp the currently-installed skill catalog into the snapshot table, throttled
 * so repeated requests don't rescan the filesystem. Call before serving trigger
 * health so version attribution reflects the present install without a startup
 * hook. Filesystem read errors are already swallowed by scanSkillCatalogs.
 */
export function refreshSkillCatalogSnapshots(nowMs: number = Date.now()): CatalogSkill[] {
  if (
    lastCatalogScan
    && nowMs - lastCatalogRefreshMs < CATALOG_REFRESH_TTL_MS
  ) {
    return lastCatalogScan;
  }
  lastCatalogRefreshMs = nowMs;
  const skills = scanSkillCatalogs(config.skillCatalogDirs);
  lastCatalogScan = skills;
  refreshCatalogSnapshots(getDb(), skills, new Date(nowMs).toISOString());
  return skills;
}

function loadCatalogSnapshots(): CatalogSnapshot[] {
  const db = getDb();
  return db.prepare(`
    SELECT name, version, first_seen_at AS firstSeenAt, last_seen_at AS lastSeenAt
    FROM skill_catalog_snapshots
  `).all() as CatalogSnapshot[];
}

interface HealthAccumulator {
  name: string;
  version: string | null;
  versionApproximate: boolean;
  invocations: number;
  lastInvokedAt: string | null;
  misfireEligible: number;
  misfires: number;
}

function healthKey(name: string, version: string | null): string {
  // NUL joins name+version into a collision-free composite Map key (no
  // printable char can appear in either half to forge the boundary). Built
  // via fromCharCode so no literal NUL sits in the source bytes: a literal
  // one makes grep/rg treat this 3.5k-line file as binary and silently
  // return nothing for every symbol below it.
  return `${name}${String.fromCharCode(0)}${version ?? ''}`;
}

function recordHealthInvocation(
  acc: Map<string, HealthAccumulator>,
  unpinnedNames: Set<string>,
  name: string,
  version: string | null,
  approximate: boolean,
  timestamp: string,
  misfire: boolean | null,
): void {
  // A null or approximate version means we couldn't pin this invocation to a
  // specific installed version; record the name so never-fired detection won't
  // claim the installed version was never invoked.
  if (version === null || approximate) unpinnedNames.add(name);
  const key = healthKey(name, version);
  const entry = acc.get(key) ?? {
    name,
    version,
    versionApproximate: false,
    invocations: 0,
    lastInvokedAt: null,
    misfireEligible: 0,
    misfires: 0,
  };
  entry.invocations += 1;
  if (approximate) entry.versionApproximate = true;
  if (!entry.lastInvokedAt || timestamp > entry.lastInvokedAt) entry.lastInvokedAt = timestamp;
  if (misfire !== null) {
    entry.misfireEligible += 1;
    if (misfire) entry.misfires += 1;
  }
  acc.set(key, entry);
}

/**
 * Per-skill trigger health: invocation counts, last-invoked, interrupt-based
 * misfire rate, and never-fired flags, attributed to the skill version installed
 * at each invocation. Computed at query time over existing tool_calls/messages/
 * events rows, so it covers historical sessions with no reingest.
 */
function buildAnalyticsSkillsHealth(
  occurrences: ReturnType<typeof selectSkillInvocationOccurrences>,
  snapshots: CatalogSnapshot[],
  catalog: CatalogSkill[],
): SkillHealthRow[] {
  const acc = new Map<string, HealthAccumulator>();
  const unpinnedNames = new Set<string>();
  const explicitInvocations = occurrences.filter(
    occurrence => occurrence.detectionSource === 'explicit_skill_tool',
  );

  const boundaries = loadUserTurnBoundaries([
    ...new Set(explicitInvocations.map(invocation => invocation.sessionId)),
  ]);

  for (const occurrence of occurrences) {
    const misfire = occurrence.detectionSource === 'explicit_skill_tool'
      && occurrence.messageOrdinal != null
      ? invocationMisfired(
        boundaries.get(occurrence.sessionId),
        occurrence.messageOrdinal,
      )
      : null;
    const resolved = resolveVersionAt(
      snapshots,
      occurrence.skillName,
      occurrence.timestamp,
    );
    recordHealthInvocation(
      acc,
      unpinnedNames,
      occurrence.skillName,
      resolved.version,
      resolved.approximate,
      occurrence.timestamp,
      misfire,
    );
  }

  const rows: SkillHealthRow[] = [...acc.values()].map(entry => ({
    name: entry.name,
    version: entry.version,
    versionApproximate: entry.versionApproximate,
    invocations: entry.invocations,
    lastInvokedAt: entry.lastInvokedAt,
    neverFired: false,
    misfireEligible: entry.misfireEligible,
    misfires: entry.misfireEligible > 0 ? entry.misfires : null,
    misfireRate: entry.misfireEligible > 0 ? entry.misfires / entry.misfireEligible : null,
  }));

  // Never-fired: installed catalog (name, version) pairs with no invocations in
  // range. Keyed by version, not name, so a freshly-installed version surfaces as
  // never-fired even while an older version of the same skill has invocations —
  // but a name with any unpinned (null/approximate) invocation is skipped, since
  // that invocation may in fact be the installed version we just can't attribute.
  for (const skill of catalog) {
    if (acc.has(healthKey(skill.name, skill.version))) continue;
    if (unpinnedNames.has(skill.name)) continue;
    rows.push({
      name: skill.name,
      version: skill.version,
      versionApproximate: false,
      invocations: 0,
      lastInvokedAt: null,
      neverFired: true,
      misfireEligible: 0,
      misfires: null,
      misfireRate: null,
    });
  }

  return rows.sort((a, b) =>
    b.invocations - a.invocations
    || (b.misfireRate ?? -1) - (a.misfireRate ?? -1)
    || a.name.localeCompare(b.name),
  );
}

export function getAnalyticsSkillsHealth(params: AnalyticsParams = {}): SkillHealthRow[] {
  const db = getDb();
  const snapshots = loadCatalogSnapshots();
  const occurrences = selectSkillInvocationOccurrences(db, params);
  const catalog = scanSkillCatalogs(config.skillCatalogDirs);
  return buildAnalyticsSkillsHealth(occurrences, snapshots, catalog);
}

export function getAnalyticsSkillConsultations(
  params: AnalyticsParams = {},
): SkillConsultationAnalytics {
  return getSkillConsultationAnalytics(
    getDb(),
    params,
    loadCatalogSnapshots(),
  );
}

export function getAnalyticsSkillHealthParts(
  params: AnalyticsParams = {},
  catalog: CatalogSkill[] = scanSkillCatalogs(config.skillCatalogDirs),
): { data: SkillHealthRow[]; consultations: SkillConsultationAnalytics } {
  const db = getDb();
  const snapshots = loadCatalogSnapshots();
  const occurrences = selectSkillInvocationOccurrences(db, params);
  return {
    data: buildAnalyticsSkillsHealth(occurrences, snapshots, catalog),
    consultations: getSkillConsultationAnalytics(
      db,
      params,
      snapshots,
      { occurrences },
    ),
  };
}

export function getAnalyticsHourOfWeek(params: AnalyticsParams = {}): HourOfWeekDataPoint[] {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params);
  const rows = db.prepare(`
    SELECT
      ((CAST(strftime('%w', started_at) AS INTEGER) + 6) % 7) as day_of_week,
      CAST(strftime('%H', started_at) AS INTEGER) as hour_of_day,
      COUNT(*) as session_count,
      COALESCE(SUM(message_count), 0) as message_count,
      COALESCE(SUM(user_message_count), 0) as user_message_count
    FROM browsing_sessions
    ${filter.where}
    GROUP BY day_of_week, hour_of_day
    ORDER BY day_of_week, hour_of_day
  `).all(...filter.values) as HourOfWeekDataPoint[];

  const byBucket = new Map(rows.map(row => [`${row.day_of_week}:${row.hour_of_day}`, row]));
  const grid: HourOfWeekDataPoint[] = [];
  for (let day = 0; day < 7; day++) {
    for (let hour = 0; hour < 24; hour++) {
      grid.push(byBucket.get(`${day}:${hour}`) ?? {
        day_of_week: day,
        hour_of_day: hour,
        session_count: 0,
        message_count: 0,
        user_message_count: 0,
      });
    }
  }
  return grid;
}

export function getAnalyticsTopSessions(params: AnalyticsParams = {}): TopSessionStat[] {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params, 'bs');
  const limit = Math.min(Math.max(params.limit ?? 10, 1), 50);

  return db.prepare(`
    SELECT
      bs.id,
      bs.project,
      bs.agent,
      bs.started_at,
      bs.ended_at,
      bs.message_count,
      bs.user_message_count,
      COALESCE(tc.tool_call_count, 0) as tool_call_count,
      bs.fidelity
    FROM browsing_sessions bs
    LEFT JOIN (
      SELECT session_id, COUNT(*) as tool_call_count
      FROM tool_calls
      GROUP BY session_id
    ) tc ON tc.session_id = bs.id
    ${filter.where}
    ORDER BY bs.message_count DESC, bs.started_at DESC, bs.id DESC
    LIMIT ?
  `).all(...filter.values, limit) as TopSessionStat[];
}

export function getAnalyticsVelocity(params: AnalyticsParams = {}): VelocityMetrics {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params);

  const row = db.prepare(`
    SELECT
      COUNT(*) as total_sessions,
      COALESCE(SUM(message_count), 0) as total_messages,
      COALESCE(SUM(user_message_count), 0) as total_user_messages,
      COUNT(DISTINCT date(started_at)) as active_days,
      MIN(started_at) as earliest,
      MAX(started_at) as latest
    FROM browsing_sessions
    ${filter.where}
  `).get(...filter.values) as {
    total_sessions: number;
    total_messages: number;
    total_user_messages: number;
    active_days: number;
    earliest: string | null;
    latest: string | null;
  };

  const spanDays = inclusiveDateSpanDays(row.earliest, row.latest);

  const safeActiveDays = Math.max(row.active_days, 1);
  const safeSpanDays = Math.max(spanDays, 1);
  const safeSessions = Math.max(row.total_sessions, 1);

  return {
    total_sessions: row.total_sessions,
    total_messages: row.total_messages,
    total_user_messages: row.total_user_messages,
    active_days: row.total_sessions > 0 ? row.active_days : 0,
    span_days: spanDays,
    sessions_per_active_day: row.total_sessions > 0 ? roundMetric(row.total_sessions / safeActiveDays) : 0,
    messages_per_active_day: row.total_sessions > 0 ? roundMetric(row.total_messages / safeActiveDays) : 0,
    sessions_per_calendar_day: row.total_sessions > 0 ? roundMetric(row.total_sessions / safeSpanDays) : 0,
    messages_per_calendar_day: row.total_sessions > 0 ? roundMetric(row.total_messages / safeSpanDays) : 0,
    average_messages_per_session: row.total_sessions > 0 ? roundMetric(row.total_messages / safeSessions) : 0,
    average_user_messages_per_session: row.total_sessions > 0 ? roundMetric(row.total_user_messages / safeSessions) : 0,
    coverage: getAnalyticsCoverage(params, 'all_sessions'),
  };
}

export function getAnalyticsAgents(params: AnalyticsParams = {}): AgentComparisonRow[] {
  const db = getDb();
  const filter = buildAnalyticsFilterState(params);
  const fidelityExpr = analyticsFidelityExpr();

  return db.prepare(`
    SELECT
      agent,
      COUNT(*) as session_count,
      COALESCE(SUM(message_count), 0) as message_count,
      COALESCE(SUM(user_message_count), 0) as user_message_count,
      ROUND(COALESCE(1.0 * SUM(message_count) / NULLIF(COUNT(*), 0), 0), 2) as average_messages_per_session,
      COALESCE(SUM(CASE WHEN ${fidelityExpr} = 'full' THEN 1 ELSE 0 END), 0) as full_fidelity_sessions,
      COALESCE(SUM(CASE WHEN ${fidelityExpr} = 'summary' THEN 1 ELSE 0 END), 0) as summary_fidelity_sessions,
      COALESCE(SUM(CASE WHEN ${toolAnalyticsCapableCondition()} THEN 1 ELSE 0 END), 0) as tool_analytics_capable_sessions,
      MIN(started_at) as first_started_at,
      MAX(started_at) as last_started_at
    FROM browsing_sessions
    ${filter.where}
    GROUP BY agent
    ORDER BY message_count DESC, session_count DESC, agent ASC
  `).all(...filter.values) as AgentComparisonRow[];
}

// --- Expected skill realizations ---

export interface SessionSkillContextSessionRow {
  id: string;
  agent: string;
  started_at: string | null;
  ended_at: string | null;
  live_status: string | null;
  skill_context_capabilities_json: string | null;
}

export interface SessionSkillContextObservationRow {
  id: number;
  ordinal: number;
  kind: string;
  source: string;
  observed_at: string | null;
  skill_name: string | null;
  command_fingerprint: string | null;
  project_identity: string | null;
  reason: string | null;
  metadata_json: string;
}

export interface SessionSkillContextCatalogEntryRow {
  observation_id: number;
  ordinal: number;
  skill_name: string;
  description: string | null;
  description_fingerprint: string | null;
  source_location: string | null;
  scope: string | null;
}

export interface SessionSkillContextInstructionEventRow {
  id: number;
  event_type: string;
  source: string;
  observed_at: string | null;
  metadata: string;
}

export function selectSessionSkillContextSession(
  db: Database,
  sessionId: string,
): SessionSkillContextSessionRow | undefined {
  return db.prepare(`
    SELECT id, agent, started_at, ended_at, live_status,
           skill_context_capabilities_json
    FROM browsing_sessions
    WHERE id = ?
  `).get(sessionId) as SessionSkillContextSessionRow | undefined;
}

export function selectSessionSkillContextObservations(
  db: Database,
  sessionId: string,
  limit: number,
): SessionSkillContextObservationRow[] {
  return db.prepare(`
    SELECT id, ordinal, kind, source, observed_at, skill_name,
           command_fingerprint, project_identity, reason, metadata_json
    FROM session_context_observations
    WHERE session_id = ?
    ORDER BY ordinal ASC, id ASC
    LIMIT ?
  `).all(sessionId, limit) as SessionSkillContextObservationRow[];
}

export function selectSessionSkillContextCatalogEntries(
  db: Database,
  sessionId: string,
  limit: number,
): SessionSkillContextCatalogEntryRow[] {
  return db.prepare(`
    SELECT entry.observation_id, entry.ordinal, entry.skill_name,
           entry.description, entry.description_fingerprint,
           entry.source_location, entry.scope
    FROM session_catalog_observation_entries entry
    JOIN session_context_observations observation
      ON observation.id = entry.observation_id
    WHERE observation.session_id = ?
      AND observation.kind = 'catalog_presentation'
    ORDER BY observation.ordinal ASC, observation.id ASC, entry.ordinal ASC
    LIMIT ?
  `).all(sessionId, limit) as SessionSkillContextCatalogEntryRow[];
}

export function selectSessionSkillContextInstructionEvents(
  db: Database,
  sessionId: string,
  limit: number,
): SessionSkillContextInstructionEventRow[] {
  return db.prepare(`
    SELECT id, event_type, source,
           COALESCE(client_timestamp, created_at) AS observed_at,
           metadata
    FROM events
    WHERE session_id = ?
      AND event_type IN ('session_start', 'instruction_load')
    ORDER BY COALESCE(client_timestamp, created_at) ASC, id ASC
    LIMIT ?
  `).all(sessionId, limit) as SessionSkillContextInstructionEventRow[];
}

export interface ExpectedRealizationPersistenceRow {
  payload_json: string;
  content_hash: string;
}

export interface ExpectedRealizationPersistenceInput {
  id: string;
  harness: string;
  profileIdentity: string;
  canonicalRevision: string;
  validFrom: string;
  validTo: string | null;
  canonicalJson: string;
  contentHash: string;
}

export function selectExpectedRealizationPersistenceRow(
  db: Database,
  realizationId: string,
): ExpectedRealizationPersistenceRow | undefined {
  return db.prepare(`
    SELECT payload_json, content_hash
    FROM skill_expected_realizations
    WHERE id = ?
  `).get(realizationId) as ExpectedRealizationPersistenceRow | undefined;
}

export function insertExpectedRealizationPersistenceRow(
  db: Database,
  input: ExpectedRealizationPersistenceInput,
): void {
  db.prepare(`
    INSERT INTO skill_expected_realizations (
      id, harness, profile_identity, canonical_revision,
      valid_from, valid_to, payload_json, content_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    input.id,
    input.harness,
    input.profileIdentity,
    input.canonicalRevision,
    input.validFrom,
    input.validTo,
    input.canonicalJson,
    input.contentHash,
  );
}

export function selectBrowsingSessionHarness(
  db: Database,
  sessionId: string,
): string | undefined {
  const row = db.prepare(`
    SELECT agent
    FROM browsing_sessions
    WHERE id = ?
  `).get(sessionId) as { agent: string } | undefined;
  return row?.agent;
}

export function selectExpectedRealizationHarness(
  db: Database,
  realizationId: string,
): string | undefined {
  const row = db.prepare(`
    SELECT harness
    FROM skill_expected_realizations
    WHERE id = ?
  `).get(realizationId) as { harness: string } | undefined;
  return row?.harness;
}

export function selectSessionExpectedRealizationId(
  db: Database,
  sessionId: string,
): string | undefined {
  const row = db.prepare(`
    SELECT realization_id
    FROM session_expected_skill_realizations
    WHERE session_id = ?
  `).get(sessionId) as { realization_id: string } | undefined;
  return row?.realization_id;
}

export function insertSessionExpectedRealizationAssociation(
  db: Database,
  sessionId: string,
  realizationId: string,
): void {
  db.prepare(`
    INSERT INTO session_expected_skill_realizations (session_id, realization_id)
    VALUES (?, ?)
  `).run(sessionId, realizationId);
}

export function selectSessionExpectedRealizationPersistenceRow(
  db: Database,
  sessionId: string,
): ExpectedRealizationPersistenceRow | undefined {
  return db.prepare(`
    SELECT realization.payload_json, realization.content_hash
    FROM session_expected_skill_realizations association
    JOIN skill_expected_realizations realization
      ON realization.id = association.realization_id
    WHERE association.session_id = ?
  `).get(sessionId) as ExpectedRealizationPersistenceRow | undefined;
}

// --- Usage ---

interface UsageFilterState {
  conditions: string[];
  values: unknown[];
  where: string;
}

interface UsageDbRow {
  session_id: string;
  source: string;
  project: string;
  agent_type: string;
  model: string;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  timestamp: string | null;
}

interface UsageRow extends UsageDbRow {
  classification: ModelClassification;
}

interface UsageAccumulator {
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  usage_events: number;
  sessions: Set<string>;
  unknown_model_events: number;
}

function usageTimestampExpr(alias = 'e'): string {
  return `COALESCE(${qualifyColumn(alias, 'client_timestamp')}, ${qualifyColumn(alias, 'created_at')})`;
}

function usageProjectExpr(alias = 'e'): string {
  return `COALESCE(NULLIF(${qualifyColumn(alias, 'project')}, ''), 'unknown')`;
}

function usageAgentExpr(alias = 'e'): string {
  return qualifyColumn(alias, 'agent_type');
}

function usageModelExpr(alias = 'e'): string {
  return `COALESCE(NULLIF(${qualifyColumn(alias, 'model')}, ''), 'unknown')`;
}

function usageMetricsCondition(alias = 'e'): string {
  return `(
    COALESCE(${qualifyColumn(alias, 'cost_usd')}, 0) > 0
    OR COALESCE(${qualifyColumn(alias, 'tokens_in')}, 0) > 0
    OR COALESCE(${qualifyColumn(alias, 'tokens_out')}, 0) > 0
    OR COALESCE(${qualifyColumn(alias, 'cache_read_tokens')}, 0) > 0
    OR COALESCE(${qualifyColumn(alias, 'cache_write_tokens')}, 0) > 0
  )`;
}

function buildUsageFilterState(params: UsageParams = {}, alias = 'e'): UsageFilterState {
  const conditions: string[] = [];
  const values: unknown[] = [];
  const timestampExpr = usageTimestampExpr(alias);

  if (params.project) {
    conditions.push(`${usageProjectExpr(alias)} = ?`);
    values.push(params.project);
  }
  if (params.agent) {
    conditions.push(`${usageAgentExpr(alias)} = ?`);
    values.push(params.agent);
  }
  if (params.date_from) {
    conditions.push(`datetime(${timestampExpr}) >= datetime(?)`);
    values.push(params.date_from);
  }
  if (params.date_to) {
    conditions.push(`datetime(${timestampExpr}) < datetime(?, '+1 day')`);
    values.push(params.date_to);
  }
  // Segregate batch-imported benchmark runs from real-activity aggregates unless
  // the caller opts in. A NULL source predates the column default and is never a
  // benchmark row, so keep it.
  if (!params.include_benchmark) {
    conditions.push(`(${alias}.source IS NULL OR ${alias}.source != 'benchmark')`);
  }

  return {
    conditions,
    values,
    where: conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '',
  };
}

function normalizeUsageFilterValue(value: string | undefined): string | null {
  const normalized = value?.trim().toLowerCase();
  return normalized ? normalized : null;
}

function hasUsageClassificationFilter(params: UsageParams): boolean {
  return Boolean(
    normalizeUsageFilterValue(params.model)
    || normalizeUsageFilterValue(params.provider)
    || normalizeUsageFilterValue(params.tier),
  );
}

function usageClassificationMatches(
  model: string,
  params: UsageParams,
  classification = classifyModelForUsage(model),
): boolean {
  const modelFilter = normalizeUsageFilterValue(params.model);
  const providerFilter = normalizeUsageFilterValue(params.provider);
  const tierFilter = normalizeUsageFilterValue(params.tier);
  if (!modelFilter && !providerFilter && !tierFilter) return true;

  if (
    modelFilter
    && model.toLowerCase() !== modelFilter
    && classification.canonical_model.toLowerCase() !== modelFilter
  ) {
    return false;
  }
  if (providerFilter && classification.provider.toLowerCase() !== providerFilter) {
    return false;
  }
  if (tierFilter && classification.tier.toLowerCase() !== tierFilter) {
    return false;
  }
  return true;
}

function preferUsageProject(...projects: Array<string | null | undefined>): string | null {
  for (const project of projects) {
    const normalized = project?.trim();
    if (normalized && normalized !== 'unknown') {
      return normalized;
    }
  }
  return null;
}

function roundCost(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function roundRate(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function createUsageAccumulator(): UsageAccumulator {
  return {
    cost_usd: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    usage_events: 0,
    sessions: new Set(),
    unknown_model_events: 0,
  };
}

function addUsageRow(acc: UsageAccumulator, row: UsageRow, classification: ModelClassification): void {
  acc.cost_usd += row.cost_usd;
  acc.input_tokens += row.tokens_in;
  acc.output_tokens += row.tokens_out;
  acc.cache_read_tokens += row.cache_read_tokens;
  acc.cache_write_tokens += row.cache_write_tokens;
  acc.usage_events += 1;
  acc.sessions.add(row.session_id);
  if (classification.pricing_status === 'unknown') {
    acc.unknown_model_events += 1;
  }
}

function usageAccumulatorToMetrics(acc: UsageAccumulator): Omit<UsageProjectBreakdown, 'project'> {
  return {
    cost_usd: roundCost(acc.cost_usd),
    input_tokens: acc.input_tokens,
    output_tokens: acc.output_tokens,
    cache_read_tokens: acc.cache_read_tokens,
    cache_write_tokens: acc.cache_write_tokens,
    usage_events: acc.usage_events,
    session_count: acc.sessions.size,
  };
}

function usageAccumulatorToBreakdown(acc: UsageAccumulator): Omit<UsageTierBreakdown, 'provider' | 'tier'> {
  return {
    ...usageAccumulatorToMetrics(acc),
    unknown_model_events: acc.unknown_model_events,
  };
}

function usageRowsToSummaryValues(rows: UsageRow[]): {
  total_cost_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_write_tokens: number;
  total_usage_events: number;
  total_sessions: number;
  active_days: number;
  earliest: string | null;
  latest: string | null;
  peak_day: { date: string | null; cost_usd: number };
} {
  const sessions = new Set<string>();
  const days = new Map<string, number>();
  let totalCost = 0;
  let totalInput = 0;
  let totalOutput = 0;
  let totalCacheRead = 0;
  let totalCacheWrite = 0;
  let earliest: string | null = null;
  let latest: string | null = null;

  for (const row of rows) {
    totalCost += row.cost_usd;
    totalInput += row.tokens_in;
    totalOutput += row.tokens_out;
    totalCacheRead += row.cache_read_tokens;
    totalCacheWrite += row.cache_write_tokens;
    sessions.add(row.session_id);
    if (row.timestamp) {
      if (!earliest || row.timestamp < earliest) earliest = row.timestamp;
      if (!latest || row.timestamp > latest) latest = row.timestamp;
      const date = row.timestamp.slice(0, 10);
      days.set(date, (days.get(date) ?? 0) + row.cost_usd);
    }
  }

  const peakDay = [...days.entries()]
    .sort((a, b) => b[1] - a[1] || b[0].localeCompare(a[0]))[0];

  return {
    total_cost_usd: roundCost(totalCost),
    total_input_tokens: totalInput,
    total_output_tokens: totalOutput,
    total_cache_read_tokens: totalCacheRead,
    total_cache_write_tokens: totalCacheWrite,
    total_usage_events: rows.length,
    total_sessions: sessions.size,
    active_days: rows.length > 0 ? days.size : 0,
    earliest,
    latest,
    peak_day: peakDay ? { date: peakDay[0], cost_usd: roundCost(peakDay[1]) } : { date: null, cost_usd: 0 },
  };
}

function selectUsageRows(params: UsageParams = {}): UsageRow[] {
  const db = getDb();
  const filter = buildUsageFilterState(params, 'e');
  const usageWhere = [
    ...filter.conditions,
    usageMetricsCondition('e'),
    excludeOverlappingCodexOtelUsageCondition('e'),
  ].join(' AND ');
  const timestampExpr = usageTimestampExpr('e');
  const rows = db.prepare(`
    SELECT
      e.session_id as session_id,
      COALESCE(NULLIF(e.source, ''), 'api') as source,
      ${usageProjectExpr('e')} as project,
      ${usageAgentExpr('e')} as agent_type,
      ${usageModelExpr('e')} as model,
      COALESCE(e.cost_usd, 0) as cost_usd,
      COALESCE(e.tokens_in, 0) as tokens_in,
      COALESCE(e.tokens_out, 0) as tokens_out,
      COALESCE(e.cache_read_tokens, 0) as cache_read_tokens,
      COALESCE(e.cache_write_tokens, 0) as cache_write_tokens,
      ${timestampExpr} as timestamp
    FROM events e
    WHERE ${usageWhere}
    ORDER BY ${timestampExpr} ASC, e.id ASC
  `).all(...filter.values) as UsageDbRow[];

  const selected: UsageRow[] = [];
  for (const row of rows) {
    const usageRow = row as UsageRow;
    usageRow.classification = classifyModelForUsage(row.model);
    if (usageClassificationMatches(row.model, params, usageRow.classification)) {
      selected.push(usageRow);
    }
  }
  return selected;
}

function selectUsageCostTotal(params: UsageParams = {}): number {
  const db = getDb();
  const filter = buildUsageFilterState(params, 'e');
  const usageWhere = [
    ...filter.conditions,
    usageMetricsCondition('e'),
    excludeOverlappingCodexOtelUsageCondition('e'),
  ].join(' AND ');

  if (!hasUsageClassificationFilter(params)) {
    const row = db.prepare(`
      SELECT COALESCE(SUM(e.cost_usd), 0) as cost_usd
      FROM events e
      WHERE ${usageWhere}
    `).get(...filter.values) as { cost_usd: number };
    return roundCost(row.cost_usd);
  }

  const groups = db.prepare(`
    SELECT
      ${usageModelExpr('e')} as model,
      COALESCE(SUM(e.cost_usd), 0) as cost_usd
    FROM events e
    WHERE ${usageWhere}
    GROUP BY ${usageModelExpr('e')}
  `).all(...filter.values) as Array<{ model: string; cost_usd: number }>;

  return roundCost(groups
    .filter(group => usageClassificationMatches(group.model, params))
    .reduce((total, group) => total + group.cost_usd, 0));
}

function estimateCacheSavings(row: UsageRow): number {
  // Use the same tier-selected rates `calculate()` billed this event at, so a
  // long-context Gemini request (prompt > 200K) reports savings against its
  // doubled long-context input rate, not the base rate.
  const rates = pricingRegistry.effectiveRates(row.model, {
    input: row.tokens_in,
    output: row.tokens_out,
    cacheRead: row.cache_read_tokens,
    cacheWrite: row.cache_write_tokens,
  });
  if (!rates) return 0;
  return (
    row.cache_read_tokens * (rates.inputCostPerToken - rates.cacheReadCostPerToken)
    + row.cache_write_tokens * (rates.inputCostPerToken - rates.cacheWriteCostPerToken)
  );
}

function resolveUsageDateBounds(
  params: UsageParams,
  earliest: string | null,
  latest: string | null,
): { from: string | null; to: string | null } {
  // The monitor UI sends full ISO timestamps (e.g. 2026-06-02T12:02:29.756Z);
  // enumerateDateRange needs bare YYYY-MM-DD or it yields an Invalid Date and
  // the whole daily series comes back empty.
  const from = params.date_from?.slice(0, 10) ?? earliest?.slice(0, 10) ?? null;
  const to = params.date_to?.slice(0, 10) ?? latest?.slice(0, 10) ?? null;
  if (!from || !to || from > to) {
    return { from: null, to: null };
  }
  return { from, to };
}

interface UsageCoverageGroup {
  source: string;
  model: string;
  session_id: string;
  event_count: number;
  usage_event_count: number;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

interface UsageMatchingGroup {
  source: string;
  model: string;
  session_id: string;
  event_count: number;
}

function isUsageCoverageGroup(
  group: UsageCoverageGroup | UsageMatchingGroup,
): group is UsageCoverageGroup {
  return 'usage_event_count' in group;
}

/**
 * Grouped in SQL rather than materialized row-by-row: coverage only ever reports
 * counts and sums, so pulling every in-range event into JS (~133k on a 30-day
 * range) to add them up was the single most expensive read on the Usage page.
 *
 * Grouping by (source, model, session_id) — not just source — is what keeps the
 * JS-side classification filter honest: provider/tier params resolve through the
 * pricing registry, which SQL cannot see, so the model has to survive into JS.
 * Carrying session_id keeps COUNT(DISTINCT session) exact across models within a
 * source. The grouped set is a few hundred rows at most.
 */
function selectUsageCoverageGroups(params: UsageParams = {}): UsageCoverageGroup[] {
  const db = getDb();
  const filter = buildUsageFilterState(params, 'e');
  const hasUsage = usageMetricsCondition('e');
  const where = [
    ...filter.conditions,
    excludeOverlappingCodexOtelUsageCondition('e'),
  ].join(' AND ');

  const rows = db.prepare(`
    SELECT
      COALESCE(NULLIF(e.source, ''), 'api') as source,
      ${usageModelExpr('e')} as model,
      e.session_id as session_id,
      COUNT(*) as event_count,
      SUM(CASE WHEN ${hasUsage} THEN 1 ELSE 0 END) as usage_event_count,
      SUM(CASE WHEN ${hasUsage} THEN COALESCE(e.cost_usd, 0) ELSE 0 END) as cost_usd,
      SUM(CASE WHEN ${hasUsage} THEN COALESCE(e.tokens_in, 0) ELSE 0 END) as tokens_in,
      SUM(CASE WHEN ${hasUsage} THEN COALESCE(e.tokens_out, 0) ELSE 0 END) as tokens_out,
      SUM(CASE WHEN ${hasUsage} THEN COALESCE(e.cache_read_tokens, 0) ELSE 0 END) as cache_read_tokens,
      SUM(CASE WHEN ${hasUsage} THEN COALESCE(e.cache_write_tokens, 0) ELSE 0 END) as cache_write_tokens
    FROM events e
    ${where ? `WHERE ${where}` : ''}
    GROUP BY source, model, e.session_id
  `).all(...filter.values) as UsageCoverageGroup[];

  return rows.filter(row => usageClassificationMatches(row.model, params));
}

function selectUsageMatchingGroups(params: UsageParams = {}): UsageMatchingGroup[] {
  const db = getDb();
  const filter = buildUsageFilterState(params, 'e');
  const where = [
    ...filter.conditions,
    excludeOverlappingCodexOtelUsageCondition('e'),
  ].join(' AND ');

  if (!hasUsageClassificationFilter(params)) {
    return db.prepare(`
      SELECT
        COALESCE(NULLIF(e.source, ''), 'api') as source,
        '' as model,
        e.session_id as session_id,
        COUNT(*) as event_count
      FROM events e
      ${where ? `WHERE ${where}` : ''}
      GROUP BY source, e.session_id
    `).all(...filter.values) as UsageMatchingGroup[];
  }

  const rows = db.prepare(`
    SELECT
      COALESCE(NULLIF(e.source, ''), 'api') as source,
      ${usageModelExpr('e')} as model,
      e.session_id as session_id,
      COUNT(*) as event_count
    FROM events e
    ${where ? `WHERE ${where}` : ''}
    GROUP BY source, model, e.session_id
  `).all(...filter.values) as UsageMatchingGroup[];

  return rows.filter(row => usageClassificationMatches(row.model, params));
}

function usageRowsToCoverageGroups(usageRows: UsageRow[]): UsageCoverageGroup[] {
  const groups = new Map<string, UsageCoverageGroup>();
  for (const row of usageRows) {
    const key = `${row.source}\0${row.model}\0${row.session_id}`;
    const group = groups.get(key) ?? {
      source: row.source,
      model: row.model,
      session_id: row.session_id,
      event_count: 0,
      usage_event_count: 0,
      cost_usd: 0,
      tokens_in: 0,
      tokens_out: 0,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
    };
    group.usage_event_count += 1;
    group.cost_usd += row.cost_usd;
    group.tokens_in += row.tokens_in;
    group.tokens_out += row.tokens_out;
    group.cache_read_tokens += row.cache_read_tokens;
    group.cache_write_tokens += row.cache_write_tokens;
    groups.set(key, group);
  }

  const compareBinary = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);
  return [...groups.values()].sort((a, b) => (
    compareBinary(a.source, b.source)
    || compareBinary(a.model, b.model)
    || compareBinary(a.session_id, b.session_id)
  ));
}

export function getUsageCoverage(params: UsageParams = {}, usageRows?: UsageRow[]): UsageCoverage {
  const groups = usageRows ? selectUsageMatchingGroups(params) : selectUsageCoverageGroups(params);

  const matchingSessions = new Set<string>();
  const usageSessions = new Set<string>();
  const sources = new Map<string, UsageSourceBreakdown & { usageSessionIds: Set<string> }>();

  let matchingEvents = 0;
  let usageEvents = 0;

  for (const group of groups) {
    matchingEvents += group.event_count;
    matchingSessions.add(group.session_id);

    const existing = sources.get(group.source) ?? {
      source: group.source,
      event_count: 0,
      usage_event_count: 0,
      session_count: 0,
      cost_usd: 0,
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      usageSessionIds: new Set<string>(),
    };
    existing.event_count += group.event_count;
    if (isUsageCoverageGroup(group)) {
      usageEvents += group.usage_event_count;
      existing.usage_event_count += group.usage_event_count;
      existing.cost_usd += group.cost_usd;
      existing.input_tokens += group.tokens_in;
      existing.output_tokens += group.tokens_out;
      existing.cache_read_tokens += group.cache_read_tokens;
      existing.cache_write_tokens += group.cache_write_tokens;
      if (group.usage_event_count > 0) {
        usageSessions.add(group.session_id);
        existing.usageSessionIds.add(group.session_id);
      }
    }
    sources.set(group.source, existing);
  }

  if (usageRows) {
    for (const group of usageRowsToCoverageGroups(usageRows)) {
      usageEvents += group.usage_event_count;
      usageSessions.add(group.session_id);
      const existing = sources.get(group.source) ?? {
        source: group.source,
        event_count: 0,
        usage_event_count: 0,
        session_count: 0,
        cost_usd: 0,
        input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        usageSessionIds: new Set<string>(),
      };
      existing.usage_event_count += group.usage_event_count;
      existing.cost_usd += group.cost_usd;
      existing.input_tokens += group.tokens_in;
      existing.output_tokens += group.tokens_out;
      existing.cache_read_tokens += group.cache_read_tokens;
      existing.cache_write_tokens += group.cache_write_tokens;
      existing.usageSessionIds.add(group.session_id);
      sources.set(group.source, existing);
    }
  }

  const sourceBreakdown = [...sources.values()]
    .map(({ usageSessionIds, ...row }) => ({
      ...row,
      session_count: usageSessionIds.size,
      cost_usd: roundCost(row.cost_usd),
    }))
    .sort((a, b) => a.source.localeCompare(b.source));

  return {
    metric_scope: 'event_usage',
    matching_events: matchingEvents,
    usage_events: usageEvents,
    missing_usage_events: Math.max(0, matchingEvents - usageEvents),
    matching_sessions: matchingSessions.size,
    usage_sessions: usageSessions.size,
    sources_with_usage: sourceBreakdown.filter(row => row.usage_event_count > 0).length,
    source_breakdown: sourceBreakdown,
    note: 'Usage is derived from ingested events with cost or token data. Sessions without usage-bearing events are excluded from totals but still reflected in coverage.',
  };
}

// The `usageRows` parameter lets getUsageOverview() feed every rollup from one
// scan instead of each re-running an identical query. Callers that want a single
// panel keep passing params alone and pay for their own scan, as before.
export function getUsageSummary(
  params: UsageParams = {},
  usageRows: UsageRow[] = selectUsageRows(params),
  sharedCoverage?: UsageCoverage,
): UsageSummary {
  const row = usageRowsToSummaryValues(usageRows);
  const spanDays = inclusiveDateSpanDays(row.earliest, row.latest);
  const safeActiveDays = Math.max(row.active_days, 1);
  const safeSessions = Math.max(row.total_sessions, 1);
  const cacheHitDenominator = row.total_input_tokens + row.total_cache_read_tokens;
  let estimatedCacheSavingsUsd = 0;
  let pricingKnownEvents = 0;
  let pricingUnknownEvents = 0;
  let unknownModelEvents = 0;

  for (const usageRow of usageRows) {
    const classification = usageRow.classification;
    if (classification.known) {
      pricingKnownEvents += 1;
    } else {
      pricingUnknownEvents += 1;
    }
    if (classification.pricing_status === 'unknown') {
      unknownModelEvents += 1;
    }
    estimatedCacheSavingsUsd += estimateCacheSavings(usageRow);
  }

  let priorTotalCostUsd = 0;
  if (params.date_from && params.date_to && params.date_from <= params.date_to) {
    const rangeDays = inclusiveDateSpanDays(params.date_from, params.date_to);
    const priorTo = addDaysToDateString(params.date_from, -1);
    const priorFrom = priorTo ? addDaysToDateString(priorTo, -(rangeDays - 1)) : null;
    if (priorFrom && priorTo) {
      priorTotalCostUsd = selectUsageCostTotal({
        ...params,
        date_from: priorFrom,
        date_to: priorTo,
      });
    }
  }
  const costDeltaPct = priorTotalCostUsd > 0
    ? roundMetric(((row.total_cost_usd - priorTotalCostUsd) / priorTotalCostUsd) * 100)
    : 0;

  return {
    total_cost_usd: row.total_cost_usd,
    total_input_tokens: row.total_input_tokens,
    total_output_tokens: row.total_output_tokens,
    total_cache_read_tokens: row.total_cache_read_tokens,
    total_cache_write_tokens: row.total_cache_write_tokens,
    total_usage_events: row.total_usage_events,
    total_sessions: row.total_sessions,
    active_days: row.total_usage_events > 0 ? row.active_days : 0,
    span_days: spanDays,
    average_cost_per_active_day: row.total_usage_events > 0 ? roundMetric(row.total_cost_usd / safeActiveDays) : 0,
    average_cost_per_session: row.total_sessions > 0 ? roundMetric(row.total_cost_usd / safeSessions) : 0,
    cache_hit_rate: cacheHitDenominator > 0 ? roundRate(row.total_cache_read_tokens / cacheHitDenominator) : 0,
    estimated_cache_savings_usd: roundCost(estimatedCacheSavingsUsd),
    pricing_known_events: pricingKnownEvents,
    pricing_unknown_events: pricingUnknownEvents,
    unknown_model_events: unknownModelEvents,
    prior_total_cost_usd: priorTotalCostUsd,
    cost_delta_pct: costDeltaPct,
    peak_day: row.peak_day,
    coverage: sharedCoverage ?? getUsageCoverage(params),
  };
}

export function getUsageDaily(
  params: UsageParams = {},
  usageRows: UsageRow[] = selectUsageRows(params),
): UsageDailyPoint[] {
  const days = new Map<string, UsageAccumulator>();
  for (const row of usageRows) {
    if (!row.timestamp) continue;
    const date = row.timestamp.slice(0, 10);
    const acc = days.get(date) ?? createUsageAccumulator();
    addUsageRow(acc, row, row.classification);
    days.set(date, acc);
  }

  const rows = [...days.entries()]
    .map(([date, acc]) => ({
      date,
      ...usageAccumulatorToMetrics(acc),
    }))
    .sort((a, b) => a.date.localeCompare(b.date));

  const bounds = resolveUsageDateBounds(
    params,
    rows[0]?.date ?? null,
    rows.length > 0 ? rows[rows.length - 1]?.date ?? null : null,
  );
  if (!bounds.from || !bounds.to) {
    return rows;
  }

  const byDate = new Map(rows.map(row => [row.date, row]));
  return enumerateDateRange(bounds.from, bounds.to).map(date => byDate.get(date) ?? {
    date,
    cost_usd: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    usage_events: 0,
    session_count: 0,
  });
}

export function getUsageProjects(
  params: UsageParams = {},
  usageRows: UsageRow[] = selectUsageRows(params),
): UsageProjectBreakdown[] {
  const projects = new Map<string, UsageAccumulator>();
  for (const row of usageRows) {
    const acc = projects.get(row.project) ?? createUsageAccumulator();
    addUsageRow(acc, row, row.classification);
    projects.set(row.project, acc);
  }

  return [...projects.entries()]
    .map(([project, acc]) => ({
      project,
      ...usageAccumulatorToMetrics(acc),
    }))
    .sort((a, b) => b.cost_usd - a.cost_usd || b.input_tokens - a.input_tokens || a.project.localeCompare(b.project));
}

function toUsageModelBreakdown(model: string, acc: UsageAccumulator): UsageModelBreakdown {
  const classification = classifyModelForUsage(model);
  return {
    model,
    ...usageAccumulatorToMetrics(acc),
    canonical_model: classification.canonical_model,
    provider: classification.provider,
    family: classification.family,
    tier: classification.tier,
    known: classification.known,
    deprecated: classification.deprecated,
    pricing_status: classification.pricing_status,
  };
}

function compareUsageModelBreakdown(a: UsageModelBreakdown, b: UsageModelBreakdown): number {
  return b.cost_usd - a.cost_usd || b.input_tokens - a.input_tokens || a.model.localeCompare(b.model);
}

export function getUsageModels(
  params: UsageParams = {},
  usageRows: UsageRow[] = selectUsageRows(params),
): UsageModelBreakdown[] {
  const models = new Map<string, UsageAccumulator>();
  for (const usageRow of usageRows) {
    const acc = models.get(usageRow.model) ?? createUsageAccumulator();
    addUsageRow(acc, usageRow, usageRow.classification);
    models.set(usageRow.model, acc);
  }

  return [...models.entries()]
    .map(([model, acc]) => toUsageModelBreakdown(model, acc))
    .sort(compareUsageModelBreakdown);
}

export function getUsageModelsDaily(
  params: UsageParams = {},
  usageRows: UsageRow[] = selectUsageRows(params),
): UsageModelDailyPoint[] {
  const days = new Map<string, Map<string, UsageAccumulator>>();
  for (const row of usageRows) {
    if (!row.timestamp) continue;
    const date = row.timestamp.slice(0, 10);
    const models = days.get(date) ?? new Map<string, UsageAccumulator>();
    const acc = models.get(row.model) ?? createUsageAccumulator();
    addUsageRow(acc, row, row.classification);
    models.set(row.model, acc);
    days.set(date, models);
  }

  const points = [...days.entries()]
    .map(([date, models]) => ({
      date,
      models: [...models.entries()]
        .map(([model, acc]) => toUsageModelBreakdown(model, acc))
        .sort(compareUsageModelBreakdown),
    }))
    .sort((a, b) => a.date.localeCompare(b.date));

  const bounds = resolveUsageDateBounds(
    params,
    points[0]?.date ?? null,
    points.length > 0 ? points[points.length - 1]?.date ?? null : null,
  );
  if (!bounds.from || !bounds.to) {
    return points;
  }

  const byDate = new Map(points.map(point => [point.date, point]));
  return enumerateDateRange(bounds.from, bounds.to).map(date => byDate.get(date) ?? { date, models: [] });
}

export function getUsageTiers(
  params: UsageParams = {},
  usageRows: UsageRow[] = selectUsageRows(params),
): UsageTierBreakdown[] {
  const tiers = new Map<string, { provider: string; tier: string; acc: UsageAccumulator }>();

  for (const row of usageRows) {
    const classification = row.classification;
    const provider = classification.provider;
    const tier = classification.tier;
    const key = `${provider}\0${tier}`;
    const existing = tiers.get(key) ?? { provider, tier, acc: createUsageAccumulator() };
    addUsageRow(existing.acc, row, classification);
    tiers.set(key, existing);
  }

  return [...tiers.values()]
    .map(({ provider, tier, acc }) => ({
      provider,
      tier,
      ...usageAccumulatorToBreakdown(acc),
    }))
    .sort((a, b) => (
      b.cost_usd - a.cost_usd
      || b.input_tokens - a.input_tokens
      || a.provider.localeCompare(b.provider)
      || a.tier.localeCompare(b.tier)
    ));
}

export function getUsageAgents(
  params: UsageParams = {},
  usageRows: UsageRow[] = selectUsageRows(params),
): UsageAgentBreakdown[] {
  const agents = new Map<string, UsageAccumulator>();
  for (const row of usageRows) {
    const acc = agents.get(row.agent_type) ?? createUsageAccumulator();
    addUsageRow(acc, row, row.classification);
    agents.set(row.agent_type, acc);
  }

  return [...agents.entries()]
    .map(([agent, acc]) => ({
      agent,
      ...usageAccumulatorToMetrics(acc),
    }))
    .sort((a, b) => b.cost_usd - a.cost_usd || b.input_tokens - a.input_tokens || a.agent.localeCompare(b.agent));
}

export function getUsageTopSessions(
  params: UsageParams = {},
  usageRows: UsageRow[] = selectUsageRows(params),
): UsageTopSessionRow[] {
  const db = getDb();
  const limit = Math.min(Math.max(params.limit ?? 10, 1), 50);
  const filter = buildUsageFilterState(params, 'e');
  const sessions = new Map<string, {
    id: string;
    project: string | null;
    agent: string;
    first_activity_at: string | null;
    last_activity_at: string | null;
    acc: UsageAccumulator;
    models: Map<string, { model: string; cost_usd: number; input_tokens: number; classification: ModelClassification }>;
    tiers: Map<string, { provider: string; tier: string; cost_usd: number; usage_events: number }>;
    unknown_model_events: number;
  }>();

  for (const usageRow of usageRows) {
    const classification = usageRow.classification;
    const entry = sessions.get(usageRow.session_id) ?? {
      id: usageRow.session_id,
      project: preferUsageProject(usageRow.project),
      agent: usageRow.agent_type,
      first_activity_at: usageRow.timestamp,
      last_activity_at: usageRow.timestamp,
      acc: createUsageAccumulator(),
      models: new Map(),
      tiers: new Map(),
      unknown_model_events: 0,
    };
    addUsageRow(entry.acc, usageRow, classification);
    entry.project = preferUsageProject(entry.project, usageRow.project);
    entry.agent = usageRow.agent_type;
    if (usageRow.timestamp) {
      if (!entry.first_activity_at || usageRow.timestamp < entry.first_activity_at) {
        entry.first_activity_at = usageRow.timestamp;
      }
      if (!entry.last_activity_at || usageRow.timestamp > entry.last_activity_at) {
        entry.last_activity_at = usageRow.timestamp;
      }
    }

    const modelKey = classification.canonical_model;
    const modelEntry = entry.models.get(modelKey) ?? {
      model: usageRow.model,
      cost_usd: 0,
      input_tokens: 0,
      classification,
    };
    modelEntry.cost_usd += usageRow.cost_usd;
    modelEntry.input_tokens += usageRow.tokens_in;
    entry.models.set(modelKey, modelEntry);

    const tierKey = `${classification.provider}\0${classification.tier}`;
    const tierEntry = entry.tiers.get(tierKey) ?? {
      provider: classification.provider,
      tier: classification.tier,
      cost_usd: 0,
      usage_events: 0,
    };
    tierEntry.cost_usd += usageRow.cost_usd;
    tierEntry.usage_events += 1;
    entry.tiers.set(tierKey, tierEntry);

    if (classification.pricing_status === 'unknown') {
      entry.unknown_model_events += 1;
    }
    sessions.set(usageRow.session_id, entry);
  }

  const entries = [...sessions.values()]
    .sort((a, b) => (
      b.acc.cost_usd - a.acc.cost_usd
      || (b.last_activity_at ?? '').localeCompare(a.last_activity_at ?? '')
      || b.id.localeCompare(a.id)
    ))
    .slice(0, limit);

  if (entries.length === 0) return [];

  const ids = entries.map(entry => entry.id);
  const placeholders = ids.map(() => '?').join(', ');
  const browsingRows = db.prepare(`
    SELECT id, project, agent, started_at, ended_at, message_count, user_message_count, fidelity
    FROM browsing_sessions
    WHERE id IN (${placeholders})
  `).all(...ids) as Array<{
    id: string;
    project: string | null;
    agent: string | null;
    started_at: string | null;
    ended_at: string | null;
    message_count: number | null;
    user_message_count: number | null;
    fidelity: string | null;
  }>;
  const browsingById = new Map(browsingRows.map(row => [row.id, row]));

  const sessionRows = db.prepare(`
    SELECT id, project, agent_type, started_at, ended_at
    FROM sessions
    WHERE id IN (${placeholders})
  `).all(...ids) as Array<{
    id: string;
    project: string | null;
    agent_type: string | null;
    started_at: string | null;
    ended_at: string | null;
  }>;
  const sessionsById = new Map(sessionRows.map(row => [row.id, row]));

  const eventCountWhere = [
    `e.session_id IN (${placeholders})`,
    ...filter.conditions,
  ].join(' AND ');
  const eventCountRows = db.prepare(`
    SELECT e.session_id as id, COUNT(*) as event_count
    FROM events e
    WHERE ${eventCountWhere}
    GROUP BY e.session_id
  `).all(...ids, ...filter.values) as Array<{ id: string; event_count: number }>;
  const eventCountsById = new Map(eventCountRows.map(row => [row.id, row.event_count]));

  return entries
    .map(entry => {
      const browsing = browsingById.get(entry.id);
      const session = sessionsById.get(entry.id);
      const primary = [...entry.models.values()].sort((a, b) => (
        b.cost_usd - a.cost_usd
        || b.input_tokens - a.input_tokens
        || a.model.localeCompare(b.model)
      ))[0];
      const tierCosts = [...entry.tiers.values()]
        .map(tier => ({
          ...tier,
          cost_usd: roundCost(tier.cost_usd),
        }))
        .sort((a, b) => (
          b.cost_usd - a.cost_usd
          || a.provider.localeCompare(b.provider)
          || a.tier.localeCompare(b.tier)
        ));

      return {
        id: entry.id,
        project: preferUsageProject(entry.project, browsing?.project, session?.project),
        agent: entry.agent ?? session?.agent_type ?? browsing?.agent ?? 'unknown',
        started_at: browsing?.started_at ?? session?.started_at ?? entry.first_activity_at,
        ended_at: browsing?.ended_at ?? session?.ended_at ?? entry.last_activity_at,
        last_activity_at: entry.last_activity_at,
        message_count: browsing?.message_count ?? null,
        user_message_count: browsing?.user_message_count ?? null,
        fidelity: browsing?.fidelity ?? null,
        cost_usd: roundCost(entry.acc.cost_usd),
        input_tokens: entry.acc.input_tokens,
        output_tokens: entry.acc.output_tokens,
        cache_read_tokens: entry.acc.cache_read_tokens,
        cache_write_tokens: entry.acc.cache_write_tokens,
        event_count: eventCountsById.get(entry.id) ?? 0,
        usage_events: entry.acc.usage_events,
        primary_model: primary?.model ?? 'unknown',
        primary_tier: primary?.classification.tier ?? 'unknown',
        primary_provider: primary?.classification.provider ?? 'unknown',
        model_count: entry.models.size,
        tier_costs: tierCosts,
        unknown_model_events: entry.unknown_model_events,
        browsing_session_available: Boolean(browsing),
      };
    });
}

/**
 * Options for the five Usage filter dropdowns.
 *
 * These were five separate calls to the full rollup endpoints (projects/agents/
 * models/tiers x2) — each scanning every usage row and pricing-classifying it —
 * purely to read off a list of distinct values. The distinct (project, agent,
 * model) tuples number in the low hundreds even across a year, so one DISTINCT
 * query answers all five.
 *
 * Each list omits its own filter (selecting a model must not collapse the model
 * dropdown to that one model) but honors the other four, which is why the facets
 * are derived per-key rather than shared. Provider and tier are pure functions of
 * the model via the pricing registry, so they need no extra query.
 */
export function getUsageFacets(params: UsageParams = {}): UsageFacets {
  const db = getDb();
  // Only dates constrain the SQL here; project/agent are applied per-facet in JS
  // below, alongside the classification filters SQL cannot express. Carry the
  // benchmark opt-in so facets stay consistent with the other usage panels.
  const filter = buildUsageFilterState(
    { date_from: params.date_from, date_to: params.date_to, include_benchmark: params.include_benchmark },
    'e',
  );
  const where = [
    ...filter.conditions,
    usageMetricsCondition('e'),
    excludeOverlappingCodexOtelUsageCondition('e'),
  ].join(' AND ');

  const rows = db.prepare(`
    SELECT DISTINCT
      ${usageProjectExpr('e')} as project,
      ${usageAgentExpr('e')} as agent,
      ${usageModelExpr('e')} as model
    FROM events e
    WHERE ${where}
  `).all(...filter.values) as Array<{ project: string; agent: string; model: string }>;

  const collect = (
    exclude: 'project' | 'agent' | 'model' | 'provider' | 'tier',
    pick: (row: { project: string; agent: string; model: string }) => string,
  ): string[] => {
    const scoped: UsageParams = { ...params };
    delete scoped[exclude];
    delete scoped.date_from;
    delete scoped.date_to;

    const values = new Set<string>();
    for (const row of rows) {
      if (scoped.project && row.project !== scoped.project) continue;
      if (scoped.agent && row.agent !== scoped.agent) continue;
      if (!usageClassificationMatches(row.model, scoped)) continue;
      const value = pick(row);
      if (value) values.add(value);
    }
    return [...values].sort((a, b) => a.localeCompare(b));
  };

  return {
    projects: collect('project', row => row.project),
    agents: collect('agent', row => row.agent),
    models: collect('model', row => row.model),
    providers: collect('provider', row => classifyModelForUsage(row.model).provider),
    tiers: collect('tier', row => classifyModelForUsage(row.model).tier),
  };
}

/**
 * Every panel on the Usage page is a different rollup of the same rows, and each
 * response carried an identical `coverage` block. Fetching them as eight requests
 * meant eight scans plus eight identical coverage computations — and because
 * better-sqlite3 is synchronous, the "parallel" requests just serialized. One
 * scan, one coverage, all rollups.
 */
export function getUsageOverview(params: UsageParams = {}): UsageOverview {
  const usageRows = selectUsageRows(params);
  const coverage = getUsageCoverage(params, usageRows);

  return {
    summary: getUsageSummary(params, usageRows, coverage),
    daily: getUsageDaily(params, usageRows),
    projects: getUsageProjects(params, usageRows),
    models: getUsageModels(params, usageRows),
    models_daily: getUsageModelsDaily(params, usageRows),
    tiers: getUsageTiers(params, usageRows),
    agents: getUsageAgents(params, usageRows),
    top_sessions: getUsageTopSessions(params, usageRows),
    coverage,
  };
}

// --- Insights ---

function parseInsightRow(row: InsightDbRow): InsightRow {
  return {
    id: row.id,
    kind: row.kind,
    title: row.title,
    prompt: row.prompt,
    content: row.content,
    date_from: row.date_from,
    date_to: row.date_to,
    project: row.project,
    agent: row.agent,
    provider: row.provider,
    model: row.model,
    analytics_summary: JSON.parse(row.analytics_summary_json) as InsightRow['analytics_summary'],
    analytics_coverage: JSON.parse(row.analytics_coverage_json) as InsightRow['analytics_coverage'],
    usage_summary: JSON.parse(row.usage_summary_json) as InsightRow['usage_summary'],
    usage_coverage: JSON.parse(row.usage_coverage_json) as InsightRow['usage_coverage'],
    input_snapshot: JSON.parse(row.input_json) as InsightInputSnapshot,
    created_at: row.created_at,
  };
}

function buildInsightsFilterState(params: InsightsListParams = {}): {
  conditions: string[];
  values: unknown[];
  where: string;
} {
  const conditions: string[] = [];
  const values: unknown[] = [];

  if (params.kind) {
    conditions.push('kind = ?');
    values.push(params.kind);
  }
  if (params.project) {
    conditions.push('project = ?');
    values.push(params.project);
  }
  if (params.agent) {
    conditions.push('agent = ?');
    values.push(params.agent);
  }
  if (params.date_from) {
    conditions.push('date_to >= ?');
    values.push(params.date_from);
  }
  if (params.date_to) {
    conditions.push('date_from <= ?');
    values.push(params.date_to);
  }

  return {
    conditions,
    values,
    where: conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '',
  };
}

export function listInsights(params: InsightsListParams = {}): InsightRow[] {
  const db = getDb();
  const filter = buildInsightsFilterState(params);
  const limit = Math.min(Math.max(params.limit ?? 50, 1), 200);

  return (db.prepare(`
    SELECT *
    FROM insights
    ${filter.where}
    ORDER BY created_at DESC, id DESC
    LIMIT ?
  `).all(...filter.values, limit) as InsightDbRow[]).map(parseInsightRow);
}

export function getInsight(id: number): InsightRow | undefined {
  const db = getDb();
  const row = db.prepare('SELECT * FROM insights WHERE id = ?').get(id) as InsightDbRow | undefined;
  return row ? parseInsightRow(row) : undefined;
}

export function createInsight(input: {
  kind: GenerateInsightParams['kind'];
  title: string;
  prompt: string | null;
  content: string;
  date_from: string;
  date_to: string;
  project: string | null;
  agent: string | null;
  provider: string;
  model: string;
  analytics_summary: InsightRow['analytics_summary'];
  analytics_coverage: InsightRow['analytics_coverage'];
  usage_summary: InsightRow['usage_summary'];
  usage_coverage: InsightRow['usage_coverage'];
  input_snapshot: InsightInputSnapshot;
}): InsightRow {
  const db = getDb();

  const result = db.prepare(`
    INSERT INTO insights (
      kind,
      title,
      prompt,
      content,
      date_from,
      date_to,
      project,
      agent,
      provider,
      model,
      analytics_summary_json,
      analytics_coverage_json,
      usage_summary_json,
      usage_coverage_json,
      input_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    input.kind,
    input.title,
    input.prompt,
    input.content,
    input.date_from,
    input.date_to,
    input.project,
    input.agent,
    input.provider,
    input.model,
    JSON.stringify(input.analytics_summary),
    JSON.stringify(input.analytics_coverage),
    JSON.stringify(input.usage_summary),
    JSON.stringify(input.usage_coverage),
    JSON.stringify(input.input_snapshot),
  );

  const created = getInsight(Number(result.lastInsertRowid));
  if (!created) {
    throw new Error('Failed to load created insight');
  }
  return created;
}

export function deleteInsight(id: number): boolean {
  const db = getDb();
  const result = db.prepare('DELETE FROM insights WHERE id = ?').run(id);
  return result.changes > 0;
}

// --- Metadata ---

export function getDistinctProjects(): string[] {
  const db = getDb();
  const rows = db.prepare(
    'SELECT DISTINCT project FROM browsing_sessions WHERE project IS NOT NULL ORDER BY project'
  ).all() as Array<{ project: string }>;
  return rows.map(r => r.project);
}

export function getDistinctAgents(): string[] {
  const db = getDb();
  const rows = db.prepare(
    'SELECT DISTINCT agent FROM browsing_sessions ORDER BY agent'
  ).all() as Array<{ agent: string }>;
  return rows.map(r => r.agent);
}

// ── Benchmarks (segregated bake-off comparison) ──────────────────────────────
// The ONE benchmark-inclusive surface: these read `source='benchmark'` in
// explicitly and never route through `buildUsageFilterState`, which keeps every
// other surface benchmark-excluding. See
// docs/specs/2026-09-02-benchmark-comparison-view-spec.md.

/** Mean-score floor below which an arm is treated as not really engaging the task. */
const BENCH_ENGAGE_FLOOR = 0.5;

interface BenchCell {
  study: string | null;
  cost_usd: number | null;
  cache_read_tokens: number | null;
  duration_ms: number | null;
  client_timestamp: string | null;
  project: string | null;
  m: Record<string, unknown>;
}

function parseBenchMeta(metadata: string | null): Record<string, unknown> {
  if (!metadata) return {};
  try {
    const v: unknown = JSON.parse(metadata);
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function benchCostBasis(cells: BenchCell[]): BenchmarkCostBasis {
  if (cells.some(c => c.cost_usd == null)) return 'unpriced';
  return cells.every(c => Boolean(c.m.cost_source)) ? 'captured' : 'derived';
}

function firstString(cells: BenchCell[], key: string): string | null {
  const hit = cells.map(c => c.m[key]).find(v => typeof v === 'string' && v.length > 0);
  return (hit as string) ?? null;
}

export function getBenchmarkStudies(): BenchmarkStudySummary[] {
  const db = getDb();
  const rows = db.prepare(`
    SELECT study_id, study, cost_usd, cache_read_tokens, duration_ms, client_timestamp, project, metadata
    FROM events WHERE source = 'benchmark' AND study_id IS NOT NULL
  `).all() as Array<{ study_id: string; study: string | null; cost_usd: number | null; cache_read_tokens: number | null; duration_ms: number | null; client_timestamp: string | null; project: string | null; metadata: string | null }>;

  const byStudy = new Map<string, BenchCell[]>();
  for (const r of rows) {
    const cell: BenchCell = { study: r.study, cost_usd: r.cost_usd, cache_read_tokens: r.cache_read_tokens, duration_ms: r.duration_ms, client_timestamp: r.client_timestamp, project: r.project, m: parseBenchMeta(r.metadata) };
    const arr = byStudy.get(r.study_id) ?? [];
    arr.push(cell);
    byStudy.set(r.study_id, arr);
  }

  const out: BenchmarkStudySummary[] = [];
  for (const [study_id, cells] of byStudy) {
    const arms = new Set(cells.map(c => `${String(c.m.canonical_model ?? '')}${String(c.m.reasoning_effort ?? '')}`));
    const stamps = cells.map(c => c.client_timestamp).filter((s): s is string => Boolean(s)).sort();
    const anyUnpriced = cells.some(c => c.cost_usd == null);
    out.push({
      study_id,
      study: cells.find(c => c.study)?.study ?? study_id,
      suite: firstString(cells, 'suite'),
      arm_count: arms.size,
      cell_count: cells.length,
      tasks: [...new Set(cells.map(c => c.project).filter((t): t is string => Boolean(t)))].sort(),
      date_from: stamps[0] ?? null,
      date_to: stamps[stamps.length - 1] ?? null,
      total_cost_usd: anyUnpriced ? null : cells.reduce((s, c) => s + (c.cost_usd ?? 0), 0),
      cost_basis: benchCostBasis(cells),
    });
  }
  out.sort((a, b) => (b.date_to ?? '').localeCompare(a.date_to ?? '') || a.study.localeCompare(b.study));
  return out;
}

/** Fill pareto / dominated_by / verdict in place from (mean_score, cost_per_trial). */
function computeBenchmarkFrontier(arms: BenchmarkArm[]): void {
  type Priced = BenchmarkArm & { cost_per_trial: number };
  const priced = arms.filter((a): a is Priced => a.cost_per_trial != null);

  for (const a of priced) {
    const dominators = priced.filter(b =>
      b !== a
      && b.mean_score >= a.mean_score && b.cost_per_trial <= a.cost_per_trial
      && (b.mean_score > a.mean_score || b.cost_per_trial < a.cost_per_trial));
    a.pareto = dominators.length === 0;
    if (dominators.length > 0) {
      // Connector points to the cheapest arm that beats it (tie: highest score).
      dominators.sort((x, y) => x.cost_per_trial - y.cost_per_trial || y.mean_score - x.mean_score);
      a.dominated_by = dominators[0].canonical_model;
    }
  }

  // value-pick: the engaging arm with the best score-per-dollar.
  let vp: Priced | null = null;
  for (const a of priced) {
    if (a.mean_score <= BENCH_ENGAGE_FLOOR || a.cost_per_trial <= 0) continue;
    if (!vp || a.mean_score / a.cost_per_trial > vp.mean_score / vp.cost_per_trial) vp = a;
  }

  for (const a of arms) {
    if (a.cost_per_trial == null) { a.verdict = 'unreliable'; a.pareto = false; continue; }
    if (a === vp) a.verdict = 'value-pick';
    else if (a.pareto && a.mean_score <= BENCH_ENGAGE_FLOOR) a.verdict = 'trivial-only';
    else if (a.pareto) a.verdict = 'on-frontier';
    else a.verdict = 'dominated';
  }
}

export function getBenchmarkStudy(studyId: string): BenchmarkStudyDetail {
  const db = getDb();
  const rows = db.prepare(`
    SELECT study, cost_usd, cache_read_tokens, duration_ms, client_timestamp, project, metadata
    FROM events WHERE source = 'benchmark' AND study_id = ?
  `).all(studyId) as Array<{ study: string | null; cost_usd: number | null; cache_read_tokens: number | null; duration_ms: number | null; client_timestamp: string | null; project: string | null; metadata: string | null }>;

  const cells: BenchCell[] = rows.map(r => ({ study: r.study, cost_usd: r.cost_usd, cache_read_tokens: r.cache_read_tokens, duration_ms: r.duration_ms, client_timestamp: r.client_timestamp, project: r.project, m: parseBenchMeta(r.metadata) }));
  // Expected cells per arm = the full task×trial grid, not the max trial index.
  // A multi-task study runs every arm against every (task, trial) combination, so
  // the max trial index alone under-counts and hides missing cells in the
  // excluded-trials honesty flag below.
  const distinctTasks = new Set(cells.map(c => c.project).filter((t): t is string => Boolean(t)));
  const distinctTrials = new Set(cells.map(c => Number(c.m.trial)).filter(t => Number.isFinite(t)));
  const expected_trials = Math.max(distinctTasks.size, 1) * distinctTrials.size;

  const groups = new Map<string, BenchCell[]>();
  for (const c of cells) {
    const key = `${String(c.m.canonical_model ?? '')}${String(c.m.reasoning_effort ?? '')}`;
    const arr = groups.get(key) ?? [];
    arr.push(c);
    groups.set(key, arr);
  }

  const arms: BenchmarkArm[] = [];
  for (const cs of groups.values()) {
    const m0 = cs[0].m;
    const canonical_model = String(m0.canonical_model ?? '');
    const reasoning_effort = typeof m0.reasoning_effort === 'string' && m0.reasoning_effort ? m0.reasoning_effort : null;
    const scored = cs.map(c => c.m.score).filter((s): s is number => typeof s === 'number');
    const anyUnpriced = cs.some(c => c.cost_usd == null);
    const isOpen = typeof m0.is_open_model === 'boolean' ? m0.is_open_model : null;
    // Prefer openbench's authoritative is_open_model; fall back to pricing-table
    // provenance only when absent. An `unknown` provider (unpriced model) is
    // treated as routed, NOT first-party — so hollow markers never over-claim.
    const native = isOpen === false ? true
      : isOpen === true ? false
      : ['openai', 'anthropic', 'google'].includes(classifyModelForUsage(canonical_model).provider);

    arms.push({
      canonical_model,
      reasoning_effort,
      label: reasoning_effort ? `${canonical_model} (${reasoning_effort})` : canonical_model,
      n: scored.length,
      mean_score: scored.length ? scored.reduce((a, b) => a + b, 0) / scored.length : 0,
      cost_per_trial: anyUnpriced ? null : cs.reduce((a, c) => a + (c.cost_usd ?? 0), 0) / cs.length,
      cost_basis: benchCostBasis(cs),
      mean_t_agent_s: cs.reduce((a, c) => a + (c.duration_ms ?? 0), 0) / cs.length / 1000,
      cache_reads: cs.reduce((a, c) => a + (c.cache_read_tokens ?? 0), 0),
      native,
      pareto: false,
      dominated_by: null,
      verdict: 'on-frontier',
      // Count *scored* cells: an unscored failure (rate-limited / errored) is
      // effectively an excluded trial, and the mean already averages only scored
      // cells — so subtract n, not the raw cell count, or the honesty flag hides it.
      excluded_trials: Math.max(0, expected_trials - scored.length),
      noop_trials: cs.filter(c => c.m.success === true && c.m.workspace_changed === false).length,
      token_basis: firstString(cs, 'token_basis'),
      usage_evidence_grade: firstString(cs, 'usage_evidence_grade'),
    });
  }

  computeBenchmarkFrontier(arms);
  arms.sort((a, b) => b.mean_score - a.mean_score || (a.cost_per_trial ?? Infinity) - (b.cost_per_trial ?? Infinity));

  return {
    study_id: studyId,
    study: cells.find(c => c.study)?.study ?? studyId,
    suite: firstString(cells, 'suite'),
    tasks: [...new Set(cells.map(c => c.project).filter((t): t is string => Boolean(t)))].sort(),
    expected_trials,
    arms,
  };
}
