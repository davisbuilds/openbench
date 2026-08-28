export const TRACE_QUALITY_OBSERVATION_TYPES = [
  'event',
  'span',
  'generation',
  'agent',
  'tool',
  'evaluator',
  'guardrail',
  'chain',
  'retriever',
  'embedding',
] as const;

export const TRACE_QUALITY_SOURCE_KINDS = [
  'event',
  'message',
  'tool_call',
  'session_turn',
  'session_item',
  'browsing_session',
  'otel_span',
  'live_item',
  'api',
] as const;

export const TRACE_QUALITY_SCORE_TARGET_TYPES = [
  'session',
  'browsing_session',
  'trace',
  'observation',
  'message',
  'event',
  'session_item',
  'tool_call',
] as const;

export const TRACE_QUALITY_SCORE_VALUE_TYPES = [
  'numeric',
  'categorical',
  'boolean',
  'text',
] as const;

export const TRACE_QUALITY_SCORE_SOURCES = [
  'human',
  'api',
  'code_evaluator',
  'llm_judge',
  'system',
] as const;

export const TRACE_QUALITY_PROMPT_REF_SOURCES = [
  'metadata',
  'skill_file',
  'agent_instruction',
  'task_template',
  'system_prompt',
  'manual',
  // Legacy values kept readable for existing local databases and seeded rows.
  'file',
  'inline',
  'skill',
  'template',
] as const;

export const TRACE_QUALITY_PAYLOAD_POLICIES = [
  'summary_only',
  'hash_only',
  'source_ref',
  'raw_allowed',
] as const;

export const TRACE_QUALITY_PROJECTION_STATUSES = [
  'projected',
  'failed',
  'skipped',
  'stale',
] as const;

export const TRACE_QUALITY_EXPORT_PROVIDERS = [
  'langfuse',
] as const;

export const TRACE_QUALITY_EXPORT_STATUSES = [
  'pending',
  'exported',
  'failed',
  'skipped',
] as const;

export const TRACE_QUALITY_COVERAGE_KEYS = [
  'has_full_transcript',
  'has_tool_details',
  'has_token_usage',
  'has_cost',
  'has_parent_child_structure',
  'has_raw_input',
  'has_raw_output',
  'has_reasoning',
  'has_prompt_refs',
  'projection_source',
  'projection_confidence',
] as const;

// Local, read-only quality/alert findings (spec Task 7). 13 aggregate/anomaly kinds plus the
// retained per-observation `observation_error` drill-down. `low_score`/`low_coverage` were renamed
// to `low_quality_score`/`low_trace_coverage`.
export const TRACE_QUALITY_FINDING_KINDS = [
  'high_error_rate',
  'tool_failure_rate',
  'model_error_rate',
  'rate_limit_events',
  'high_latency_p95',
  'latency_spike',
  'token_spike',
  'cost_anomaly',
  'daily_budget_risk',
  'unknown_pricing',
  'low_trace_coverage',
  'collector_or_otel_dropoff',
  'low_quality_score',
  'observation_error',
] as const;

// Ordered ascending by severity rank; index doubles as the rank (info=0 … critical=3).
export const TRACE_QUALITY_FINDING_SEVERITIES = [
  'info',
  'warning',
  'high',
  'critical',
] as const;

// Default thresholds/windows for finding computation. Overridable via a local JSON file
// (AGENTMONITOR_TRACE_QUALITY_FINDINGS_PATH). `rate` values are fractions in [0,1]; `*_ms` are
// milliseconds; `*_usd` are dollars. `daily_budget_risk` delegates to the usage-budgets module and
// `observation_error` is unconditional, so neither carries thresholds here.
export interface TraceQualityFindingThresholds {
  high_error_rate: { warning: number; high: number; critical: number; min_observations: number };
  tool_failure_rate: { warning: number; high: number; critical: number; min_calls: number };
  model_error_rate: { warning: number; high: number; critical: number; min_calls: number };
  rate_limit_events: { warning: number; high: number; critical: number };
  high_latency_p95: { warning_ms: number; high_ms: number; critical_ms: number; min_samples: number };
  latency_spike: { warning_ratio: number; high_ratio: number; min_samples_per_window: number; min_days: number };
  token_spike: { warning_ratio: number; high_ratio: number; min_baseline_days: number };
  cost_anomaly: { warning_ratio: number; high_ratio: number; critical_ratio: number; min_baseline_days: number; min_baseline_avg_usd: number };
  unknown_pricing: { warning: number; high: number; min_observations: number };
  low_trace_coverage: { warning: number; high: number; min_traces: number };
  collector_or_otel_dropoff: { warning_minutes: number; high_minutes: number };
  low_quality_score: { warning: number; high: number; critical: number };
  baseline_window_days: number;
  impacted_id_cap: number;
}

export const DEFAULT_TRACE_QUALITY_FINDING_THRESHOLDS: TraceQualityFindingThresholds = {
  high_error_rate: { warning: 0.10, high: 0.25, critical: 0.50, min_observations: 20 },
  tool_failure_rate: { warning: 0.20, high: 0.40, critical: 0.60, min_calls: 10 },
  model_error_rate: { warning: 0.10, high: 0.25, critical: 0.50, min_calls: 20 },
  rate_limit_events: { warning: 1, high: 5, critical: 20 },
  high_latency_p95: { warning_ms: 30000, high_ms: 60000, critical_ms: 120000, min_samples: 20 },
  latency_spike: { warning_ratio: 2.0, high_ratio: 3.0, min_samples_per_window: 10, min_days: 2 },
  token_spike: { warning_ratio: 2.5, high_ratio: 4.0, min_baseline_days: 3 },
  cost_anomaly: { warning_ratio: 2.5, high_ratio: 4.0, critical_ratio: 6.0, min_baseline_days: 3, min_baseline_avg_usd: 0.01 },
  unknown_pricing: { warning: 0.10, high: 0.30, min_observations: 10 },
  low_trace_coverage: { warning: 0.20, high: 0.50, min_traces: 5 },
  collector_or_otel_dropoff: { warning_minutes: 60, high_minutes: 1440 },
  low_quality_score: { warning: 0.5, high: 0.25, critical: 0.1 },
  baseline_window_days: 7,
  impacted_id_cap: 50,
};
