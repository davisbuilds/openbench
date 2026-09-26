import type {
  TRACE_QUALITY_COVERAGE_KEYS,
  TRACE_QUALITY_EXPORT_PROVIDERS,
  TRACE_QUALITY_EXPORT_STATUSES,
  TRACE_QUALITY_FINDING_KINDS,
  TRACE_QUALITY_FINDING_SEVERITIES,
  TRACE_QUALITY_OBSERVATION_TYPES,
  TRACE_QUALITY_PAYLOAD_POLICIES,
  TRACE_QUALITY_PROJECTION_STATUSES,
  TRACE_QUALITY_PROMPT_REF_SOURCES,
  TRACE_QUALITY_SCORE_SOURCES,
  TRACE_QUALITY_SCORE_TARGET_TYPES,
  TRACE_QUALITY_SCORE_VALUE_TYPES,
  TRACE_QUALITY_SOURCE_KINDS,
} from './constants.js';

export type TraceQualityObservationType = typeof TRACE_QUALITY_OBSERVATION_TYPES[number];
export type TraceQualitySourceKind = typeof TRACE_QUALITY_SOURCE_KINDS[number];
export type TraceQualityScoreTargetType = typeof TRACE_QUALITY_SCORE_TARGET_TYPES[number];
export type TraceQualityScoreValueType = typeof TRACE_QUALITY_SCORE_VALUE_TYPES[number];
export type TraceQualityScoreSource = typeof TRACE_QUALITY_SCORE_SOURCES[number];
export type TraceQualityPromptRefSource = typeof TRACE_QUALITY_PROMPT_REF_SOURCES[number];
export type TraceQualityPayloadPolicy = typeof TRACE_QUALITY_PAYLOAD_POLICIES[number];
export type TraceQualityProjectionStatus = typeof TRACE_QUALITY_PROJECTION_STATUSES[number];
export type TraceQualityExportProvider = typeof TRACE_QUALITY_EXPORT_PROVIDERS[number];
export type TraceQualityExportStatus = typeof TRACE_QUALITY_EXPORT_STATUSES[number];
export type TraceQualityCoverageKey = typeof TRACE_QUALITY_COVERAGE_KEYS[number];
export type TraceQualityFindingKind = typeof TRACE_QUALITY_FINDING_KINDS[number];
export type TraceQualityFindingSeverity = typeof TRACE_QUALITY_FINDING_SEVERITIES[number];
export type { TraceQualityFindingThresholds } from './constants.js';

export type TraceQualityStatus = 'success' | 'error' | 'timeout' | 'running' | 'pending' | 'unknown';
export type TraceQualitySeverity = 'info' | 'warning' | 'error' | 'critical';

export interface TraceQualityCoverage {
  has_full_transcript?: boolean;
  has_tool_details?: boolean;
  has_token_usage?: boolean;
  has_cost?: boolean;
  has_parent_child_structure?: boolean;
  has_raw_input?: boolean;
  has_raw_output?: boolean;
  has_reasoning?: boolean;
  has_prompt_refs?: boolean;
  projection_source?: string;
  projection_confidence?: 'high' | 'medium' | 'low' | 'unknown';
  [key: string]: unknown;
}

export interface TraceQualityTraceRow {
  id: string;
  session_id: string;
  browsing_session_id: string | null;
  source_trace_id: string | null;
  agent_type: string;
  name: string;
  status: TraceQualityStatus | string | null;
  project: string | null;
  branch: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  metadata_json: string;
  tags_json: string;
  coverage_json: string;
  created_at: string;
}

export interface TraceQualityObservationRow {
  id: string;
  trace_id: string;
  parent_observation_id: string | null;
  session_id: string;
  source_kind: TraceQualitySourceKind;
  source_id: string | null;
  source_item_id: string | null;
  observation_type: TraceQualityObservationType;
  name: string;
  status: TraceQualityStatus | string | null;
  status_message: string | null;
  severity: TraceQualitySeverity | string | null;
  model: string | null;
  tool_name: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  tokens_in: number;
  tokens_out: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number | null;
  input_hash: string | null;
  output_hash: string | null;
  input_summary: string | null;
  output_summary: string | null;
  payload_policy: TraceQualityPayloadPolicy;
  metadata_json: string;
  created_at: string;
}

export interface TraceQualityScoreRow {
  id: number;
  target_type: TraceQualityScoreTargetType;
  target_id: string;
  name: string;
  value_type: TraceQualityScoreValueType;
  numeric_value: number | null;
  categorical_value: string | null;
  boolean_value: number | null;
  text_value: string | null;
  source: TraceQualityScoreSource;
  evaluator_name: string | null;
  comment: string | null;
  metadata_json: string;
  created_at: string;
}

export interface TraceQualityPromptRefRow {
  id: number;
  name: string;
  version: string | null;
  label: string | null;
  source: TraceQualityPromptRefSource;
  content_hash: string | null;
  file_path: string | null;
  metadata_json: string;
  created_at: string;
}

export interface TraceQualityObservationPromptRow {
  observation_id: string;
  prompt_ref_id: number;
  created_at: string;
}

export interface TraceQualityProjectionStateRow {
  source_table: string;
  source_id: string;
  projection_version: string;
  trace_id: string | null;
  observation_id: string | null;
  payload_hash: string | null;
  status: TraceQualityProjectionStatus;
  projected_at: string | null;
  error_message: string | null;
  metadata_json: string;
  created_at: string;
}

export interface TraceQualityExportStateRow {
  id: number;
  provider: TraceQualityExportProvider;
  local_trace_id: string;
  local_observation_id: string | null;
  external_trace_id: string | null;
  external_observation_id: string | null;
  payload_hash: string | null;
  status: TraceQualityExportStatus;
  exported_at: string | null;
  error_message: string | null;
  metadata_json: string;
  created_at: string;
}
