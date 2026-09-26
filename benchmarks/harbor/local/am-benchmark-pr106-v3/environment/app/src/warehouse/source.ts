import { getDb } from '../db/connection.js';

export interface WarehouseSessionTraceSummaryRow {
  session_id: string;
  trace_id: string | null;
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
  projection_version: string;
  updated_at: string;
}

export interface WarehouseSummaryListParams {
  date_from?: string;
  date_to?: string;
}

function addDaysToDateString(date: string, days: number): string | null {
  const parsed = new Date(`${date}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  parsed.setUTCDate(parsed.getUTCDate() + days);
  return parsed.toISOString().slice(0, 10);
}

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

export function listWarehouseSessionTraceSummaries(
  params: WarehouseSummaryListParams = {},
): WarehouseSessionTraceSummaryRow[] {
  const conditions: string[] = [];
  const values: unknown[] = [];
  appendDateRangeConditions(
    conditions,
    values,
    'COALESCE(started_at, updated_at)',
    params.date_from,
    params.date_to,
  );
  const where = conditions.length ? `WHERE ${conditions.join(' AND ')}` : '';
  return getDb().prepare(`
    SELECT
      session_id,
      trace_id,
      agent_type,
      project,
      primary_model,
      started_at,
      ended_at,
      observation_count,
      error_count,
      tokens_in,
      tokens_out,
      cache_read_tokens,
      cache_write_tokens,
      cost_usd,
      latency_ms_total,
      coverage_json,
      quality_score,
      quality_grade,
      projection_version,
      updated_at
    FROM session_trace_summary
    ${where}
    ORDER BY datetime(COALESCE(started_at, updated_at)) ASC, session_id ASC
  `).all(...values) as WarehouseSessionTraceSummaryRow[];
}
