/**
 * On-demand trace-quality read layer (reframe Phase 2).
 *
 * The lean view presents ONE trace per session. The list is served straight from
 * the persisted `session_trace_summary` rollup; per-session detail is projected
 * in-memory on request — never persisted. This is a pure read seam: it reuses
 * the existing `projectTraceQuality` projection but keeps only its *observations*
 * (always per event/item) and re-hangs them under a single synthesized session
 * trace, so the historical per-event mis-grain in `projectEventTraces` is
 * irrelevant and left untouched. The shared projection, the persist path, and
 * the frontend contract are all unchanged.
 */
import { getDb } from '../db/connection.js';
import type {
  TraceQualityObservation,
  TraceQualityObservationListParams,
  TraceQualityObservationTreeNode,
  TraceQualityReadCoverage,
  TraceQualityTrace,
  TraceQualityTraceDetail,
  TraceQualityTraceListParams,
} from '../api/v2/types.js';
import {
  projectTraceQuality,
  type ProjectedTraceQualityObservation,
} from './projection.js';
import { readTraceQualityProjectionInputForSession } from './source-readers.js';
import type { TraceQualityCoverage } from './types.js';

/** Shift a `YYYY-MM-DD` date string by whole days (UTC). */
function addDaysToDateString(date: string, days: number): string | null {
  const parsed = new Date(`${date}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  parsed.setUTCDate(parsed.getUTCDate() + days);
  return parsed.toISOString().slice(0, 10);
}

/**
 * Append date-range conditions, treating a date-only `date_to` as the exclusive
 * next day so timestamped rows on that day are included (not dropped by a bare
 * `<= 'YYYY-MM-DD'`).
 */
function appendDateRangeConditions(
  conditions: string[],
  values: unknown[],
  column: string,
  dateFrom: string | undefined,
  dateTo: string | undefined,
): void {
  if (dateFrom) {
    conditions.push(`datetime(${column}) >= datetime(?)`);
    values.push(dateFrom);
  }
  if (dateTo) {
    const nextDay = /^\d{4}-\d{2}-\d{2}$/.test(dateTo) ? addDaysToDateString(dateTo, 1) : null;
    if (nextDay) {
      conditions.push(`datetime(${column}) < datetime(?)`);
      values.push(nextDay);
    } else {
      conditions.push(`datetime(${column}) <= datetime(?)`);
      values.push(dateTo);
    }
  }
}

interface SessionTraceSummaryRow {
  session_id: string;
  trace_id: string;
  agent_type: string | null;
  project: string | null;
  primary_model: string | null;
  started_at: string | null;
  ended_at: string | null;
  observation_count: number;
  error_count: number;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  latency_ms_total: number;
  coverage_json: string;
  quality_score: number | null;
  quality_grade: string | null;
  updated_at: string;
}

function parseJsonRecord(value: string | null | undefined): Record<string, unknown> {
  if (!value) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function normalizedLimit(value: number | undefined, fallback: number, max: number): number {
  if (value == null || Number.isNaN(value) || value <= 0) return fallback;
  return Math.min(Math.floor(value), max);
}

function normalizedOffset(value: number | undefined): number {
  if (value == null || Number.isNaN(value) || value < 0) return 0;
  return Math.floor(value);
}

/** Build the WHERE clause for the summary-backed list from the supported filters. */
function buildSummaryWhere(params: TraceQualityTraceListParams): { where: string; values: unknown[] } {
  const clauses: string[] = [];
  const values: unknown[] = [];
  if (params.session_id) {
    clauses.push('session_id = ?');
    values.push(params.session_id);
  }
  if (params.project) {
    clauses.push('project = ?');
    values.push(params.project);
  }
  if (params.agent) {
    clauses.push('agent_type = ?');
    values.push(params.agent);
  }
  // Reuse the shared helper so a date-only `date_to` is treated as the exclusive
  // next day (otherwise `<= 'YYYY-MM-DD'` drops every timestamped row on that day).
  appendDateRangeConditions(clauses, values, 'COALESCE(started_at, updated_at)', params.date_from, params.date_to);
  return { where: clauses.length ? `WHERE ${clauses.join(' AND ')}` : '', values };
}

/** A session's outcome status, derived from its error count (no status column). */
function summaryStatus(row: SessionTraceSummaryRow): string {
  if (row.observation_count === 0) return 'unknown';
  return row.error_count > 0 ? 'error' : 'success';
}

function mapSummaryToTrace(row: SessionTraceSummaryRow): TraceQualityTrace {
  return {
    id: row.trace_id,
    session_id: row.session_id,
    browsing_session_id: null,
    source_trace_id: null,
    agent_type: row.agent_type ?? 'unknown',
    name: `Session ${row.session_id}`,
    status: summaryStatus(row),
    project: row.project,
    branch: null,
    started_at: row.started_at,
    ended_at: row.ended_at,
    duration_ms: row.latency_ms_total,
    metadata: { primary_model: row.primary_model, quality_grade: row.quality_grade },
    tags: ['session'],
    coverage: parseJsonRecord(row.coverage_json),
    created_at: row.updated_at,
    aggregate: {
      observation_count: row.observation_count,
      error_count: row.error_count,
      total_tokens_in: row.tokens_in,
      total_tokens_out: row.tokens_out,
      total_cache_read_tokens: row.cache_read_tokens,
      total_cache_write_tokens: row.cache_write_tokens,
      total_cost_usd: row.cost_usd,
      total_duration_ms: row.latency_ms_total,
      first_observation_at: row.started_at,
      last_observation_at: row.ended_at,
    },
    score_count: 0,
    numeric_score_avg: null,
  };
}

type CoverageRow = Pick<SessionTraceSummaryRow, 'observation_count' | 'coverage_json'>;

/** Honest read-coverage over the FULL filtered set of summary rows (not a page). */
function buildReadCoverage(rows: readonly CoverageRow[]): TraceQualityReadCoverage {
  let withUsage = 0;
  let totalObservations = 0;
  for (const row of rows) {
    totalObservations += row.observation_count;
    const coverage = parseJsonRecord(row.coverage_json) as TraceQualityCoverage;
    if (coverage.has_token_usage === true) withUsage += row.observation_count;
  }
  return {
    matching_traces: rows.length,
    included_traces: rows.length,
    excluded_low_coverage_traces: 0,
    observations_with_usage: withUsage,
    observations_missing_usage: Math.max(0, totalObservations - withUsage),
    score_coverage: {
      scored_traces: 0,
      unscored_traces: rows.length,
      total_scores: 0,
      trace_score_count: 0,
      observation_score_count: 0,
      numeric_score_count: 0,
    },
    note: 'Lean local view: one trace per session from session_trace_summary; detail projected on-demand. Scores live in the deferred export.',
  };
}

/** List one trace per session straight from the summary rollup. */
export function listSessionTraces(params: TraceQualityTraceListParams = {}): {
  data: TraceQualityTrace[];
  total: number;
  limit: number;
  offset: number;
  coverage: TraceQualityReadCoverage;
} {
  const db = getDb();
  const limit = normalizedLimit(params.limit, 50, 500);
  const offset = normalizedOffset(params.offset);
  const { where, values } = buildSummaryWhere(params);

  const total = (db.prepare(`SELECT COUNT(*) AS c FROM session_trace_summary ${where}`).get(...values) as { c: number }).c;
  const rows = db.prepare(`
    SELECT * FROM session_trace_summary
    ${where}
    ORDER BY datetime(COALESCE(started_at, updated_at)) DESC, session_id DESC
    LIMIT ? OFFSET ?
  `).all(...values, limit, offset) as SessionTraceSummaryRow[];

  // Coverage describes the full filtered selection, not just the current page —
  // the summary table is ~one row per session, so the extra scan is cheap.
  const coverageRows = db.prepare(`
    SELECT observation_count, coverage_json FROM session_trace_summary ${where}
  `).all(...values) as CoverageRow[];

  return {
    data: rows.map(mapSummaryToTrace),
    total,
    limit,
    offset,
    coverage: buildReadCoverage(coverageRows),
  };
}

/** Resolve a stable trace_id back to its summary row. */
function summaryForTraceId(traceId: string): SessionTraceSummaryRow | undefined {
  return getDb()
    .prepare('SELECT * FROM session_trace_summary WHERE trace_id = ?')
    .get(traceId) as SessionTraceSummaryRow | undefined;
}

/** Per-session trace detail, served from the summary (no on-demand projection needed). */
export function getSessionTraceDetail(traceId: string): { trace: TraceQualityTraceDetail; coverage: TraceQualityReadCoverage } | null {
  const row = summaryForTraceId(traceId);
  if (!row) return null;
  return {
    trace: {
      ...mapSummaryToTrace(row),
      prompt_refs: [],
      score_summary: [],
    },
    coverage: buildReadCoverage([row]),
  };
}

/** Re-grain a session's projection observations under one synthesized session trace. */
function mapProjectedObservation(obs: ProjectedTraceQualityObservation, traceId: string): TraceQualityObservation {
  const { metadata_json, ...rest } = obs as ProjectedTraceQualityObservation & { metadata_json: string };
  return {
    ...rest,
    trace_id: traceId,
    created_at: obs.started_at ?? new Date().toISOString(),
    metadata: parseJsonRecord(metadata_json),
  };
}

function buildObservationTree(observations: TraceQualityObservation[]): TraceQualityObservationTreeNode[] {
  const nodes = new Map<string, TraceQualityObservationTreeNode>();
  const roots: TraceQualityObservationTreeNode[] = [];
  for (const observation of observations) {
    nodes.set(observation.id, { ...observation, children: [] });
  }
  for (const observation of observations) {
    const node = nodes.get(observation.id);
    if (!node) continue;
    const parent = observation.parent_observation_id ? nodes.get(observation.parent_observation_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

/**
 * Project a session on-demand and return its observations under the single
 * session trace. Returns null when the trace_id resolves to no session.
 */
export function listSessionObservations(
  traceId: string,
  params: TraceQualityObservationListParams = {},
): {
  data: TraceQualityObservation[];
  tree: TraceQualityObservationTreeNode[];
  total: number;
  limit: number;
  offset: number;
  coverage: TraceQualityReadCoverage;
} | null {
  const row = summaryForTraceId(traceId);
  if (!row) return null;

  const limit = normalizedLimit(params.limit, 500, 1000);
  const offset = normalizedOffset(params.offset);

  const projection = projectTraceQuality(readTraceQualityProjectionInputForSession(row.session_id));
  // Keep only observations; re-hang them all under this session's single trace
  // (the projection's own per-event trace grouping is ignored — that mis-grain
  // is what the lean view exists to fix). Order matches the persisted endpoint.
  const observations = projection.observations
    .map(obs => mapProjectedObservation(obs, traceId))
    .sort((a, b) => {
      const at = a.started_at ?? a.created_at;
      const bt = b.started_at ?? b.created_at;
      if (at < bt) return -1;
      if (at > bt) return 1;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });

  const page = observations.slice(offset, offset + limit);
  return {
    data: page,
    tree: buildObservationTree(page),
    total: observations.length,
    limit,
    offset,
    coverage: buildReadCoverage([row]),
  };
}
