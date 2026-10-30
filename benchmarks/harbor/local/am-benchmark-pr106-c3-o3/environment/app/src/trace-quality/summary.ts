/**
 * session_trace_summary — the lean, content-free, export-shaped per-session
 * rollup that replaces the persisted trace/observation warehouse for list and
 * aggregate views (see docs/specs/2026-06-29-trace-quality-reframe-spec.md).
 *
 * One row per session: token/cost/latency rollups, telemetry coverage, and a
 * single derived quality scalar. No message text — this row is safe to export
 * (its columns map to medallion's `silver.agent_runs`). The full observation
 * tree is NOT persisted; detail is projected on-demand (Phase 2).
 */
import { createHash } from 'node:crypto';

import type { Database } from 'better-sqlite3';

import { getDb } from '../db/connection.js';
import {
  coverageForEvents,
  projectTraceQuality,
  type EventProjectionSource,
  type ProjectedTraceQualityObservation,
  type ProjectedTraceQualityTrace,
} from './projection.js';
import { readTraceQualityProjectionInputForSession } from './source-readers.js';
import type { TraceQualityCoverage } from './types.js';

/**
 * Version stamp for the summary derivation (distinct from the projection
 * version). Bump it whenever `deriveSessionTraceSummary` / the quality formula
 * changes; the startup guard re-backfills every row whose stored version
 * differs, so derivation changes propagate without a manual reset.
 */
// v2: usage is rolled up from events (authoritative) even when detail structure
// comes from live session_items, fixing zeroed Codex usage (PR #37 review).
// v3: add the stable per-session `trace_id` (reframe Phase 2 on-demand read
// layer); the bump re-backfills existing rows so the column populates on upgrade.
const SESSION_TRACE_SUMMARY_VERSION = 'sts:v3';

/**
 * Stable, deterministic trace id for a session. The lean view presents one trace
 * per session; this id is persisted on `session_trace_summary` so the on-demand
 * read layer can emit it (the list row's `id`) and reverse it back to a session
 * for the detail/observation endpoints. A non-reversible hash, so the reverse
 * lookup is a column read, not a recomputation.
 */
export function sessionTraceId(sessionId: string): string {
  return `sts:${createHash('sha1').update(sessionId).digest('hex').slice(0, 24)}`;
}

export interface SessionTraceSummary {
  session_id: string;
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
  coverage: TraceQualityCoverage;
  quality_score: number | null;
  quality_grade: string | null;
}

const CONFIDENCE_WEIGHT: Record<string, number> = { high: 1, medium: 0.8, low: 0.6, unknown: 0.6 };

/**
 * Merge the coverage flags across a session's traces: a flag is present if any
 * trace has it; confidence is the lowest seen (most conservative).
 */
function mergeCoverage(traces: readonly ProjectedTraceQualityTrace[]): TraceQualityCoverage {
  const merged: TraceQualityCoverage = {};
  const confidenceRank: Record<string, number> = { high: 3, medium: 2, low: 1, unknown: 0 };
  let lowestConfidence: string | null = null;
  let source: string | undefined;

  for (const trace of traces) {
    let coverage: TraceQualityCoverage;
    try {
      coverage = JSON.parse(trace.coverage_json) as TraceQualityCoverage;
    } catch {
      coverage = {};
    }
    for (const [key, value] of Object.entries(coverage)) {
      if (key === 'projection_confidence' || key === 'projection_source') continue;
      if (value === true) merged[key] = true;
      else if (merged[key] === undefined) merged[key] = value;
    }
    source ??= coverage.projection_source;
    const conf = coverage.projection_confidence ?? 'unknown';
    if (lowestConfidence === null || (confidenceRank[conf] ?? 0) < (confidenceRank[lowestConfidence] ?? 0)) {
      lowestConfidence = conf;
    }
  }
  if (source) merged.projection_source = source;
  merged.projection_confidence = (lowestConfidence ?? 'unknown') as TraceQualityCoverage['projection_confidence'];
  return merged;
}

/**
 * v1 quality scalar — a transparent, deterministic proxy, NOT a real eval.
 * It blends outcome success (1 − error rate) with telemetry fidelity (coverage
 * breadth × projection confidence). Meaningful evaluation depth is deferred to
 * the export layer (Langfuse / medallion's fluency KPI); this is only a
 * lightweight local signal and the export contract. Range [0, 1].
 */
function computeQualityScalar(
  observationCount: number,
  errorCount: number,
  coverage: TraceQualityCoverage,
): { score: number | null; grade: string | null } {
  if (observationCount === 0) return { score: null, grade: null };

  const successRate = 1 - errorCount / observationCount;
  const confidence = coverage.projection_confidence ?? 'unknown';
  const confidenceWeight = CONFIDENCE_WEIGHT[confidence] ?? 0.6;
  const signalFlags = [
    coverage.has_token_usage === true,
    coverage.has_cost === true,
    coverage.has_tool_details === true,
    coverage.has_parent_child_structure === true,
    coverage.has_full_transcript === true || coverage.has_raw_input === true || coverage.has_raw_output === true,
  ];
  const coverageScore = signalFlags.filter(Boolean).length / signalFlags.length;
  const fidelity = confidenceWeight * (0.5 + 0.5 * coverageScore);
  const score = Math.round(successRate * fidelity * 1000) / 1000;
  const grade = score >= 0.9 ? 'A' : score >= 0.75 ? 'B' : score >= 0.6 ? 'C' : score >= 0.4 ? 'D' : 'F';
  return { score, grade };
}

function isError(observation: ProjectedTraceQualityObservation): boolean {
  return observation.severity === 'error' || observation.status === 'error';
}

interface MeasureRollup {
  count: number;
  tokensIn: number;
  tokensOut: number;
  cacheRead: number;
  cacheWrite: number;
  cost: number;
  latency: number;
  errorCount: number;
  primaryModel: string | null;
  startedAt: string | null;
  endedAt: string | null;
  coverage: TraceQualityCoverage;
}

/**
 * Roll up usage from the session's events. Events are the authoritative usage
 * source: `projectTraceQuality` structures detail from turns/items for Codex/
 * Claude sessions and ignores `input.events`, but those item observations carry
 * zero token/cost — the real usage lives on the event rows. So whenever a
 * session has events, the summary measures and coverage come from them (this
 * also makes the full derive agree with the O(1) incremental event path).
 */
function rollupFromEvents(events: readonly EventProjectionSource[]): MeasureRollup {
  const rollup: MeasureRollup = {
    count: events.length,
    tokensIn: 0, tokensOut: 0, cacheRead: 0, cacheWrite: 0, cost: 0, latency: 0, errorCount: 0,
    primaryModel: null, startedAt: null, endedAt: null, coverage: coverageForEvents(events),
  };
  const modelCounts = new Map<string, number>();
  for (const event of events) {
    rollup.tokensIn += event.tokens_in;
    rollup.tokensOut += event.tokens_out;
    rollup.cacheRead += event.cache_read_tokens;
    rollup.cacheWrite += event.cache_write_tokens;
    rollup.cost += event.cost_usd ?? 0;
    rollup.latency += event.duration_ms ?? 0;
    if (event.status === 'error') rollup.errorCount += 1;
    if (event.model) modelCounts.set(event.model, (modelCounts.get(event.model) ?? 0) + 1);
    const ts = event.client_timestamp ?? event.created_at;
    if (ts && (rollup.startedAt === null || ts < rollup.startedAt)) rollup.startedAt = ts;
    if (ts && (rollup.endedAt === null || ts > rollup.endedAt)) rollup.endedAt = ts;
  }
  rollup.primaryModel = [...modelCounts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] ?? null;
  return rollup;
}

/** Fallback rollup for sessions with no events (e.g. Claude JSONL message-sourced). */
function rollupFromObservations(
  observations: readonly ProjectedTraceQualityObservation[],
  traces: readonly ProjectedTraceQualityTrace[],
): MeasureRollup {
  const rollup: MeasureRollup = {
    count: observations.length,
    tokensIn: 0, tokensOut: 0, cacheRead: 0, cacheWrite: 0, cost: 0, latency: 0, errorCount: 0,
    primaryModel: null, startedAt: null, endedAt: null, coverage: mergeCoverage(traces),
  };
  const modelCounts = new Map<string, number>();
  for (const observation of observations) {
    rollup.tokensIn += observation.tokens_in;
    rollup.tokensOut += observation.tokens_out;
    rollup.cacheRead += observation.cache_read_tokens;
    rollup.cacheWrite += observation.cache_write_tokens;
    rollup.cost += observation.cost_usd ?? 0;
    rollup.latency += observation.duration_ms ?? 0;
    if (isError(observation)) rollup.errorCount += 1;
    if (observation.model) modelCounts.set(observation.model, (modelCounts.get(observation.model) ?? 0) + 1);
    if (observation.started_at && (rollup.startedAt === null || observation.started_at < rollup.startedAt)) rollup.startedAt = observation.started_at;
    const end = observation.ended_at ?? observation.started_at;
    if (end && (rollup.endedAt === null || end > rollup.endedAt)) rollup.endedAt = end;
  }
  rollup.primaryModel = [...modelCounts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] ?? null;
  return rollup;
}

/** Project a session in-memory and aggregate it into a content-free summary. */
export function deriveSessionTraceSummary(sessionId: string): SessionTraceSummary {
  const input = readTraceQualityProjectionInputForSession(sessionId);
  const events = input.events ?? [];
  let rollup: MeasureRollup;
  if (events.length > 0) {
    rollup = rollupFromEvents(events);
  } else {
    const projected = projectTraceQuality(input);
    rollup = rollupFromObservations(projected.observations, projected.traces);
  }

  const { score, grade } = computeQualityScalar(rollup.count, rollup.errorCount, rollup.coverage);

  return {
    session_id: sessionId,
    agent_type: input.agentType ?? null,
    project: input.project ?? null,
    primary_model: rollup.primaryModel,
    started_at: input.browsingSession?.started_at ?? rollup.startedAt,
    ended_at: input.browsingSession?.ended_at ?? rollup.endedAt,
    observation_count: rollup.count,
    error_count: rollup.errorCount,
    tokens_in: rollup.tokensIn,
    tokens_out: rollup.tokensOut,
    cache_read_tokens: rollup.cacheRead,
    cache_write_tokens: rollup.cacheWrite,
    cost_usd: Math.round(rollup.cost * 1e6) / 1e6,
    latency_ms_total: rollup.latency,
    coverage: rollup.coverage,
    quality_score: score,
    quality_grade: grade,
  };
}

interface EventRowForSummary {
  session_id: string;
  agent_type: string | null;
  project: string | null;
  model: string | null;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number | null;
  duration_ms: number | null;
  status: string | null;
  event_type: string | null;
  tool_name: string | null;
  ts: string | null;
}

/**
 * O(1) incremental summary maintenance for a single ingested event. Used on the
 * live (event-sourced) ingest path, where a full re-derive per event would be
 * O(n^2) over a session. Event-sourced sessions have no session_items, so their
 * coverage is exactly the OR-accumulation of per-event flags (mirrors
 * `coverageForEvents`); the quality scalar is recomputed from the running row.
 * Sessions that carry items/messages are maintained by the session hook instead.
 */
export function bumpSessionTraceSummaryForEvent(eventId: number): void {
  const db = getDb();
  const event = db.prepare(`
    SELECT session_id, agent_type, project, model, tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,
           cost_usd, duration_ms, status, event_type, tool_name,
           COALESCE(client_timestamp, created_at) AS ts
    FROM events WHERE id = ?
  `).get(eventId) as EventRowForSummary | undefined;
  if (!event) return;

  const hasTokens = event.tokens_in > 0 || event.tokens_out > 0 || event.cache_read_tokens > 0 || event.cache_write_tokens > 0;
  const hasCost = event.cost_usd != null;
  const hasToolDetails = event.event_type === 'tool_use' && Boolean(event.tool_name);
  const isErr = event.status === 'error' ? 1 : 0;

  db.prepare(`
    INSERT INTO session_trace_summary (
      session_id, trace_id, agent_type, project, primary_model, started_at, ended_at,
      observation_count, error_count, tokens_in, tokens_out,
      cache_read_tokens, cache_write_tokens, cost_usd, latency_ms_total,
      coverage_json, quality_score, quality_grade, projection_version, updated_at
    ) VALUES (
      @session_id, @trace_id, @agent_type, @project, @model, @ts, @ts,
      1, @is_err, @tokens_in, @tokens_out,
      @cache_read_tokens, @cache_write_tokens, @cost_usd, @duration,
      @coverage_json, NULL, NULL, @projection_version, datetime('now')
    )
    ON CONFLICT(session_id) DO UPDATE SET
      project = COALESCE(session_trace_summary.project, excluded.project),
      primary_model = COALESCE(session_trace_summary.primary_model, excluded.primary_model),
      started_at = MIN(COALESCE(session_trace_summary.started_at, excluded.started_at), COALESCE(excluded.started_at, session_trace_summary.started_at)),
      ended_at = MAX(COALESCE(session_trace_summary.ended_at, excluded.ended_at), COALESCE(excluded.ended_at, session_trace_summary.ended_at)),
      observation_count = session_trace_summary.observation_count + 1,
      error_count = session_trace_summary.error_count + excluded.error_count,
      tokens_in = session_trace_summary.tokens_in + excluded.tokens_in,
      tokens_out = session_trace_summary.tokens_out + excluded.tokens_out,
      cache_read_tokens = session_trace_summary.cache_read_tokens + excluded.cache_read_tokens,
      cache_write_tokens = session_trace_summary.cache_write_tokens + excluded.cache_write_tokens,
      cost_usd = session_trace_summary.cost_usd + excluded.cost_usd,
      latency_ms_total = session_trace_summary.latency_ms_total + excluded.latency_ms_total,
      updated_at = datetime('now')
  `).run({
    session_id: event.session_id,
    trace_id: sessionTraceId(event.session_id),
    agent_type: event.agent_type,
    project: event.project,
    model: event.model,
    ts: event.ts,
    is_err: isErr,
    tokens_in: event.tokens_in,
    tokens_out: event.tokens_out,
    cache_read_tokens: event.cache_read_tokens,
    cache_write_tokens: event.cache_write_tokens,
    cost_usd: event.cost_usd ?? 0,
    duration: event.duration_ms ?? 0,
    coverage_json: JSON.stringify({
      has_token_usage: hasTokens,
      has_cost: hasCost,
      has_tool_details: hasToolDetails,
      projection_source: 'events',
      projection_confidence: hasTokens || hasCost ? 'medium' : 'low',
    } satisfies TraceQualityCoverage),
    projection_version: SESSION_TRACE_SUMMARY_VERSION,
  });

  // OR-accumulate the coverage flags and recompute the quality scalar from the
  // running totals (all O(1) against the single summary row).
  const row = db.prepare(`
    SELECT observation_count, error_count, coverage_json FROM session_trace_summary WHERE session_id = ?
  `).get(event.session_id) as { observation_count: number; error_count: number; coverage_json: string };
  const prior = JSON.parse(row.coverage_json) as TraceQualityCoverage;
  const coverage: TraceQualityCoverage = {
    has_token_usage: prior.has_token_usage === true || hasTokens,
    has_cost: prior.has_cost === true || hasCost,
    has_tool_details: prior.has_tool_details === true || hasToolDetails,
    projection_source: 'events',
    projection_confidence:
      prior.has_token_usage === true || prior.has_cost === true || hasTokens || hasCost ? 'medium' : 'low',
  };
  const { score, grade } = computeQualityScalar(row.observation_count, row.error_count, coverage);
  db.prepare('UPDATE session_trace_summary SET coverage_json = ?, quality_score = ?, quality_grade = ? WHERE session_id = ?')
    .run(JSON.stringify(coverage), score, grade, event.session_id);
}

function upsertSessionTraceSummary(db: Database, summary: SessionTraceSummary): void {
  db.prepare(`
    INSERT INTO session_trace_summary (
      session_id, trace_id, agent_type, project, primary_model, started_at, ended_at,
      observation_count, error_count, tokens_in, tokens_out,
      cache_read_tokens, cache_write_tokens, cost_usd, latency_ms_total,
      coverage_json, quality_score, quality_grade, projection_version, updated_at
    ) VALUES (
      @session_id, @trace_id, @agent_type, @project, @primary_model, @started_at, @ended_at,
      @observation_count, @error_count, @tokens_in, @tokens_out,
      @cache_read_tokens, @cache_write_tokens, @cost_usd, @latency_ms_total,
      @coverage_json, @quality_score, @quality_grade, @projection_version, datetime('now')
    )
    ON CONFLICT(session_id) DO UPDATE SET
      trace_id = excluded.trace_id,
      agent_type = excluded.agent_type,
      project = excluded.project,
      primary_model = excluded.primary_model,
      started_at = excluded.started_at,
      ended_at = excluded.ended_at,
      observation_count = excluded.observation_count,
      error_count = excluded.error_count,
      tokens_in = excluded.tokens_in,
      tokens_out = excluded.tokens_out,
      cache_read_tokens = excluded.cache_read_tokens,
      cache_write_tokens = excluded.cache_write_tokens,
      cost_usd = excluded.cost_usd,
      latency_ms_total = excluded.latency_ms_total,
      coverage_json = excluded.coverage_json,
      quality_score = excluded.quality_score,
      quality_grade = excluded.quality_grade,
      projection_version = excluded.projection_version,
      updated_at = datetime('now')
  `).run({
    session_id: summary.session_id,
    trace_id: sessionTraceId(summary.session_id),
    agent_type: summary.agent_type,
    project: summary.project,
    primary_model: summary.primary_model,
    started_at: summary.started_at,
    ended_at: summary.ended_at,
    observation_count: summary.observation_count,
    error_count: summary.error_count,
    tokens_in: summary.tokens_in,
    tokens_out: summary.tokens_out,
    cache_read_tokens: summary.cache_read_tokens,
    cache_write_tokens: summary.cache_write_tokens,
    cost_usd: summary.cost_usd,
    latency_ms_total: summary.latency_ms_total,
    coverage_json: JSON.stringify(summary.coverage),
    quality_score: summary.quality_score,
    quality_grade: summary.quality_grade,
    projection_version: SESSION_TRACE_SUMMARY_VERSION,
  });
}

/** Derive + persist the summary for one session. Safe to call repeatedly. */
export function maintainSessionTraceSummary(sessionId: string): void {
  upsertSessionTraceSummary(getDb(), deriveSessionTraceSummary(sessionId));
}

/**
 * Run the one-time backfill at startup if the summary table is empty but
 * sessions exist (i.e. an existing DB upgrading into the reframe). Self-healing
 * and idempotent: once populated it is a single cheap probe.
 */
export function ensureSessionTraceSummaryBackfill(): void {
  const db = getDb();
  // Re-backfill when the table is empty (but sessions exist) OR when any row is
  // incompletely migrated: a stale projection_version, or a NULL trace_id that a
  // partial/interrupted backfill could have left behind. Checking a single row's
  // version is not enough — one up-to-date row would mask stale/NULL siblings,
  // leaving the table permanently un-openable (NULL trace_id ⇒ no detail).
  const total = (db.prepare('SELECT COUNT(*) AS c FROM session_trace_summary').get() as { c: number }).c;
  const incomplete = (db.prepare(
    `SELECT COUNT(*) AS c FROM session_trace_summary
     WHERE projection_version IS NOT ? OR trace_id IS NULL`,
  ).get(SESSION_TRACE_SUMMARY_VERSION) as { c: number }).c;
  if (total > 0 && incomplete === 0) return; // fully migrated

  const hasSessions =
    db.prepare('SELECT 1 FROM browsing_sessions LIMIT 1').get() ?? db.prepare('SELECT 1 FROM events LIMIT 1').get();
  if (!hasSessions) return;
  const count = backfillSessionTraceSummaries(db);
  if (count > 0) console.log(`[trace-quality] (re)built ${count} session trace summaries (${SESSION_TRACE_SUMMARY_VERSION})`);
}

/** One-shot/idempotent backfill of every session's summary. */
export function backfillSessionTraceSummaries(db: Database = getDb()): number {
  const sessionIds = new Set<string>();
  for (const row of db.prepare('SELECT id FROM browsing_sessions').all() as Array<{ id: string }>) {
    sessionIds.add(row.id);
  }
  for (const row of db.prepare('SELECT DISTINCT session_id FROM events').all() as Array<{ session_id: string }>) {
    sessionIds.add(row.session_id);
  }
  let count = 0;
  for (const sessionId of sessionIds) {
    upsertSessionTraceSummary(db, deriveSessionTraceSummary(sessionId));
    count += 1;
  }
  return count;
}
