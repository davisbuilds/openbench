export interface WarehouseRunRow {
  account: string;
  session_id: string;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  latency_ms: number;
  observation_count: number;
  error_count: number;
  quality_score: number | null;
  quality_grade: string | null;
  project: string | null;
  agent_type: string | null;
  started_at: string;
  day: string;
  published_run_id: string;
}

export interface PublishLineage {
  run_id: string;
  created_at: string;
  account: string;
  window_start: string | null;
  window_end: string | null;
  sessions_published: number;
  sessions_suppressed: number;
  min_batch: number;
  amon_version: string;
  grant_role: string | null;
  grant_skipped: boolean;
}

export interface WarehouseConfig {
  enabled: boolean;
  dsn: string | null;
  account: string;
  schema: string;
  biRole: string | null;
}
