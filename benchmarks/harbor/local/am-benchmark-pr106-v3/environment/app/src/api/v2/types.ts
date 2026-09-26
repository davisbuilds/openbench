import type { ProjectionCapabilities } from '../../live/projector.js';
import type {
  TraceQualityFindingKind,
  TraceQualityFindingSeverity,
  TraceQualityObservationRow,
  TraceQualityPromptRefRow,
  TraceQualityScoreRow,
  TraceQualityTraceRow,
} from '../../trace-quality/types.js';

// --- Database row types (also used as API response shapes) ---

export interface BrowsingSessionRow {
  id: string;
  project: string | null;
  agent: string;
  first_message: string | null;
  started_at: string | null;
  ended_at: string | null;
  message_count: number;
  user_message_count: number;
  parent_session_id: string | null;
  relationship_type: string | null;
  live_status: string | null;
  last_item_at: string | null;
  integration_mode: string | null;
  fidelity: string | null;
  capabilities: ProjectionCapabilities | null;
  file_path: string | null;
  file_size: number | null;
  file_hash: string | null;
  // Context-window occupancy (live sessions). Raw columns from the projection;
  // context_pct is derived at read time. All null when occupancy is unavailable.
  context_used_tokens: number | null;
  context_window_tokens: number | null;
  context_pct: number | null;
}

// context_pct is derived, not stored, so it is omitted from the raw DB row.
export interface BrowsingSessionDbRow
  extends Omit<BrowsingSessionRow, 'capabilities' | 'context_pct'> {
  capabilities_json: string | null;
}

export type LiveSessionRow = BrowsingSessionRow;

export interface LiveTurnRow {
  id: number;
  session_id: string;
  agent_type: string;
  source_turn_id: string | null;
  status: string | null;
  title: string | null;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
}

export interface LiveItemRow {
  id: number;
  session_id: string;
  turn_id: number | null;
  ordinal: number;
  source_item_id: string | null;
  kind: string;
  status: string | null;
  payload_json: string;
  created_at: string | null;
}

export interface LivePlanStep {
  id?: string;
  label: string;
  status?: string;
}

export interface LivePlanState {
  summary?: string;
  steps: LivePlanStep[];
}

export interface MessageRow {
  id: number;
  session_id: string;
  ordinal: number;
  role: string;
  content: string; // JSON-serialized content blocks
  timestamp: string | null;
  has_thinking: number; // 0 or 1
  has_tool_use: number; // 0 or 1
  content_length: number;
}

export interface ToolCallRow {
  id: number;
  message_id: number;
  session_id: string;
  tool_name: string;
  category: string | null;
  tool_use_id: string | null;
  input_json: string | null;
  result_content: string | null;
  result_content_length: number | null;
  subagent_session_id: string | null;
}

export interface SessionActivityBucket {
  bucket_index: number;
  start_ordinal: number | null;
  end_ordinal: number | null;
  message_count: number;
  user_message_count: number;
  assistant_message_count: number;
  first_timestamp: string | null;
  last_timestamp: string | null;
}

export interface SessionActivity {
  bucket_count: number;
  total_messages: number;
  first_timestamp: string | null;
  last_timestamp: string | null;
  timestamped_messages: number;
  untimestamped_messages: number;
  navigation_basis: 'timestamp' | 'ordinal' | 'mixed';
  data: SessionActivityBucket[];
}

export interface PinnedMessageRow {
  id: number;
  session_id: string;
  message_id: number | null;
  message_ordinal: number;
  role: string | null;
  content: string | null;
  message_timestamp: string | null;
  created_at: string;
  session_project: string | null;
  session_agent: string | null;
  session_first_message: string | null;
}

// --- Aggregate query result ---

export interface CountResult {
  c: number;
}

// --- Analytics response types ---

export interface AnalyticsSummary {
  total_sessions: number;
  total_messages: number;
  total_user_messages: number;
  daily_average_sessions: number;
  daily_average_messages: number;
  date_range: {
    earliest: string | null;
    latest: string | null;
  };
  coverage: AnalyticsCoverage;
}

export interface ActivityDataPoint {
  date: string;
  sessions: number;
  messages: number;
  user_messages: number;
}

export interface ProjectBreakdown {
  project: string;
  session_count: number;
  message_count: number;
  user_message_count: number;
}

export interface ToolUsageStat {
  tool_name: string;
  category: string | null;
  count: number;
}

export interface MonitorToolStat {
  tool_name: string;
  total_calls: number;
  error_count: number;
  error_rate: number;
  avg_duration_ms: number | null;
  by_agent: Record<string, number>;
}

export interface MonitorSessionRow {
  id: string;
  agent_id: string;
  agent_type: string;
  project: string | null;
  branch: string | null;
  status: string;
  started_at: string;
  ended_at: string | null;
  last_event_at: string;
  metadata: string | null;
  mode: 'interactive' | 'headless' | null;
  event_count: number;
  tokens_in: number;
  tokens_out: number;
  total_cost_usd: number;
  files_edited: number;
  lines_added: number;
  lines_removed: number;
}

export interface MonitorEventRow {
  id: number;
  event_id: string | null;
  schema_version: number;
  session_id: string;
  agent_type: string;
  event_type: string;
  tool_name: string | null;
  status: string;
  tokens_in: number;
  tokens_out: number;
  branch: string | null;
  project: string | null;
  duration_ms: number | null;
  created_at: string;
  client_timestamp: string | null;
  metadata: string | null;
  payload_truncated: number;
  model: string | null;
  cost_usd: number | null;
  cache_read_tokens: number;
  cache_write_tokens: number;
  source: string;
}

export interface MonitorQuotaWindow {
  used_percent: number;
  remaining_percent: number;
  resets_at: string | null;
  window_minutes: number | null;
}

export interface MonitorQuotaCredits {
  has_credits: boolean;
  unlimited: boolean;
  balance: string | null;
}

export interface MonitorQuotaSnapshot {
  provider: 'claude' | 'codex';
  agent_type: 'claude_code' | 'codex';
  status: 'available' | 'unavailable' | 'error';
  source: string | null;
  updated_at: string | null;
  account_label: string | null;
  plan_type: string | null;
  limit_id: string | null;
  limit_name: string | null;
  error_message: string | null;
  primary: MonitorQuotaWindow | null;
  secondary: MonitorQuotaWindow | null;
  credits: MonitorQuotaCredits | null;
}

export interface MonitorStats {
  total_events: number;
  active_sessions: number;
  live_sessions: number;
  total_sessions: number;
  active_agents: number;
  total_tokens_in: number;
  total_tokens_out: number;
  total_cost_usd: number;
  tool_breakdown: Record<string, number>;
  agent_breakdown: Record<string, number>;
  model_breakdown: Record<string, number>;
  branches: string[];
  quota_monitor: MonitorQuotaSnapshot[];
  usage_monitor: MonitorQuotaSnapshot[];
}

export interface MonitorFilterOptions {
  agent_types: string[];
  event_types: string[];
  tool_names: string[];
  models: string[];
  projects: string[];
  branches: Array<{ value: string; label: string }>;
  sources: string[];
}

export interface MonitorTranscriptEvent {
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
  metadata: string | null;
}

export interface MonitorTranscriptEntry {
  role: 'system' | 'user' | 'assistant' | 'tool';
  type: string;
  tool_name?: string;
  detail?: string;
  status?: string;
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  duration_ms?: number;
  timestamp: string;
}

export interface MonitorTranscriptRow {
  role: string;
  content: string;
  timestamp?: string;
}

export interface SkillUsageBreakdown {
  skill_name: string;
  count: number;
}

export interface SkillUsageDay {
  date: string;
  total: number;
  skills: SkillUsageBreakdown[];
}

export interface SkillHealthRow {
  name: string;
  /** Version installed when the skill was invoked (or currently installed, for
   *  never-fired skills); null when it cannot be resolved from the catalog. */
  version: string | null;
  /** True when at least one invocation's version was an approximate fallback. */
  versionApproximate: boolean;
  /** Total invocations in range (explicit Skill calls + Codex SKILL.md reads). */
  invocations: number;
  lastInvokedAt: string | null;
  neverFired: boolean;
  /** Invocations that could be assessed for misfire — explicit Skill calls with
   *  a linked assistant turn. Excludes Codex reads (no turn linkage). The
   *  denominator behind misfireRate; use it to weight the rate by sample size. */
  misfireEligible: number;
  /** Misfiring explicit invocations; null when none are misfire-eligible
   *  (e.g. a skill invoked only via Codex, which has no assistant-turn linkage). */
  misfires: number | null;
  /** misfires / misfireEligible; null when none are eligible. */
  misfireRate: number | null;
}

export interface SkillHealthCompatibilityRow extends SkillHealthRow {
  /** The row uses the phase-1 session-start window and must not be consumed as
   *  rich consultation analytics when multiple harnesses are pooled. */
  compatibilityOnly: boolean;
  /** False for mixed-harness rows because detection semantics differ. */
  crossHarnessComparable: boolean;
}

export type SkillConsultationClass =
  | 'first_read'
  | 'rehydration_after_compaction'
  | 'repeat_no_compaction'
  | 'unclassifiable';

export interface SkillConsultationClassCounts {
  first_read: number;
  rehydration_after_compaction: number;
  repeat_no_compaction: number;
  unclassifiable: number;
}

export interface SkillConsultationVersionRow {
  version: string | null;
  attribution: 'exact' | 'approximate' | 'unknown';
  invocations: number;
  classes: SkillConsultationClassCounts;
}

export interface SkillConsultationRow {
  name: string;
  harness: string;
  invocations: number;
  classes: SkillConsultationClassCounts;
  sessionsInWindow: number;
  eligibleSessionsInWindow: number;
  sessionsWithFirstRead: number;
  firstReadEngagementRate: number | null;
  ineligibleSessionsByReason: Array<{ reason: string; sessions: number }>;
  projectBreadth: {
    distinctObservedProjects: number;
    sessions: Array<{ id: string; label: string; sessions: number }>;
  };
  versions: SkillConsultationVersionRow[];
  exposure: {
    jointlyEligiblePresentedSessions: number;
    presentedWithFirstRead: number;
    presentedWithoutFirstRead: number;
  };
}

export interface SkillConsultationComparability {
  status: 'single_harness' | 'comparable' | 'not_directly_comparable';
  limitingEvidence: string[];
}

export interface SkillConsultationAnalytics {
  asOf: string;
  windowSemantics: {
    interval: 'utc_half_open';
    from: string | null;
    toExclusive: string | null;
    sessionMembership: 'observed_interval_overlap_or_in_window_occurrence';
    windowMembershipUnobservable: number;
  };
  byHarness: Array<{
    harness: string;
    detectionSemantics: 'explicit_skill_tool' | 'concrete_skill_path';
    skills: SkillConsultationRow[];
  }>;
  comparability: SkillConsultationComparability;
}

export interface SkillHealthDataSemantics {
  data: 'phase_1_compatibility';
  window: 'session_start_legacy';
  compatibilityOnly: boolean;
  crossHarnessComparable: boolean;
}

export interface SkillHealthResponse {
  data: SkillHealthCompatibilityRow[];
  coverage: AnalyticsCoverage;
  dataSemantics: SkillHealthDataSemantics;
  consultations: SkillConsultationAnalytics;
}

export type SkillExpectedRealizationHarness = 'claude' | 'codex';

export interface SkillExpectedRealizationMemberInput {
  name: string;
  descriptionFingerprint: string;
  version?: string | null;
  contentIdentity?: string | null;
}

export interface SkillExpectedRealizationMember {
  name: string;
  descriptionFingerprint: string;
  version: string | null;
  contentIdentity: string | null;
}

export interface SkillExpectedRealizationProvenance {
  producer: string;
  producerVersion: string;
  artifactId: string;
  artifactRevision: string;
  sourceUri: string;
}

interface SkillExpectedPolicyArtifactBase {
  artifactId: string;
  artifactRevision: string;
  contentHash: string;
  harness: SkillExpectedRealizationHarness;
  harnessVersion: string;
  model: string | null;
  modelVersion: string | null;
  contextWindowIdentity: string;
  runtimeRepresentation: string;
  limitValue: number;
  limitUnit: string;
  measurementMethod: string;
  observedAt: string;
  expiresAt: string;
  producer: string;
  producerVersion: string;
}

export interface SkillExpectedVendorPolicyArtifact
  extends SkillExpectedPolicyArtifactBase {
  kind: 'vendor_policy_snapshot';
  sourceUri: string;
  probeIdentity?: never;
}

export interface SkillExpectedProbePolicyArtifact
  extends SkillExpectedPolicyArtifactBase {
  kind: 'version_scoped_probe';
  probeIdentity: string;
  sourceUri?: never;
}

export type SkillExpectedPolicyArtifact =
  | SkillExpectedVendorPolicyArtifact
  | SkillExpectedProbePolicyArtifact;

export interface SkillExpectedRealizationInput {
  id: string;
  harness: SkillExpectedRealizationHarness;
  profileIdentity: string;
  profileComposition: string[];
  canonicalRevision: string;
  validFrom: string;
  validTo?: string | null;
  skills: SkillExpectedRealizationMemberInput[];
  policyArtifacts?: SkillExpectedPolicyArtifact[];
  provenance: SkillExpectedRealizationProvenance;
}

export interface SkillExpectedRealization
  extends Omit<SkillExpectedRealizationInput, 'validTo' | 'skills' | 'policyArtifacts'> {
  validTo: string | null;
  skills: SkillExpectedRealizationMember[];
  policyArtifacts: SkillExpectedPolicyArtifact[];
  contentHash: string;
}

export interface ExpectedRealizationValidationIssue {
  path: string;
  code: string;
  message: string;
}

export type ExpectedRealizationCreateResponse =
  | {
      ok: true;
      status: 'created' | 'replayed';
      realization: SkillExpectedRealization;
    }
  | {
      ok: false;
      status: 'invalid';
      code: 'invalid_expected_realization';
      issues: ExpectedRealizationValidationIssue[];
    }
  | {
      ok: false;
      status: 'conflict';
      code: 'expected_realization_content_conflict';
      existingContentHash: string;
    };

export type ExpectedRealizationAssociationResponse =
  | {
      ok: true;
      status: 'associated' | 'replayed';
      sessionId: string;
      realizationId: string;
    }
  | {
      ok: false;
      status: 'not_found';
      code: 'session_not_found' | 'expected_realization_not_found';
    }
  | {
      ok: false;
      status: 'invalid';
      code: 'invalid_association';
      issues: ExpectedRealizationValidationIssue[];
    }
  | {
      ok: false;
      status: 'unprocessable';
      code: 'expected_realization_harness_mismatch';
      sessionHarness: string;
      realizationHarness: string;
    }
  | {
      ok: false;
      status: 'conflict';
      code: 'session_expected_realization_conflict';
      existingRealizationId: string;
    };

export interface JsonObjectValidationErrorResponse {
  ok: false;
  status: 'invalid';
  code: 'invalid_json_object';
  issues: ExpectedRealizationValidationIssue[];
}

export interface ResourceIdMismatchResponse {
  ok: false;
  status: 'invalid';
  code: 'resource_id_mismatch';
  issues: ExpectedRealizationValidationIssue[];
}

export type ExpectedRealizationPutResponse =
  | ExpectedRealizationCreateResponse
  | JsonObjectValidationErrorResponse
  | ResourceIdMismatchResponse;

export type ExpectedRealizationAssociationPutResponse =
  | ExpectedRealizationAssociationResponse
  | JsonObjectValidationErrorResponse;

export type SkillContextCapability =
  | { observable: true }
  | { observable: false; reason: string };

export interface SessionSkillContextObservation {
  id: number;
  ordinal: number;
  kind: 'consultation' | 'compaction' | 'catalog_presentation' | 'instruction_load';
  source: string;
  observedAt: string | null;
  skillName: string | null;
  commandFingerprint: string | null;
  projectIdentity: string | null;
  reason: string | null;
  consultationClass: SkillConsultationClass | null;
}

export interface SkillCatalogPresentationEntry {
  ordinal: number;
  name: string;
  description: string | null;
  descriptionFingerprint: string | null;
  sourceLocation: string | null;
  sourceScope: string | null;
}

export interface SkillCatalogPresentationMeasurement {
  value: number;
  unit: string;
  method: string;
  exact: true;
}

export interface SkillExpectedRealizationReference {
  id: string;
  contentHash: string;
  profileIdentity: string;
  canonicalRevision: string;
  validFrom: string;
  validTo: string | null;
  provenance: SkillExpectedRealizationProvenance;
}

export type SkillExpectedComparison =
  | {
      status: 'compared';
      realization: SkillExpectedRealizationReference;
      matching: string[];
      omitted: string[];
      unexpected: string[];
      descriptionMismatched: Array<{
        name: string;
        expectedFingerprint: string;
        observedFingerprints: Array<string | null>;
      }>;
    }
  | {
      status: 'unavailable';
      reason:
        | 'no_expected_realization'
        | 'invalid_expected_realization'
        | 'presentation_unobservable'
        | 'occurrence_timestamp_unavailable'
        | 'realization_not_valid_for_occurrence';
    };

export type SkillCatalogBudget =
  | {
      status: 'available';
      used: number;
      limit: number;
      ratio: number;
      unit: string;
      measurementMethod: string;
      policyArtifactId: string;
      policyArtifactRevision: string;
      policyArtifactHash: string;
    }
  | {
      status: 'unknown';
      reason:
        | 'measurement_unavailable'
        | 'no_authoritative_limit'
        | 'limit_authority_unrecognized'
        | 'limit_authority_ambiguous'
        | 'policy_not_fresh'
        | 'policy_scope_mismatch'
        | 'incompatible_units'
        | 'incompatible_measurement_methods';
    };

export interface SkillCatalogPresentationOccurrence {
  observationId: number;
  ordinal: number;
  source: string;
  observedAt: string | null;
  projectIdentity: string | null;
  fingerprint: string;
  entries: SkillCatalogPresentationEntry[];
  measurement: SkillCatalogPresentationMeasurement | null;
  truncation: 'observed' | 'not_observed' | 'unknown';
  runtime: {
    harnessVersion: string | null;
    model: string | null;
    modelVersion: string | null;
    contextWindowIdentity: string | null;
    representation: string | null;
  };
  comparison: SkillExpectedComparison;
  budget: SkillCatalogBudget;
}

export type SkillCatalogPresentationState =
  | {
      observable: true;
      occurrences: SkillCatalogPresentationOccurrence[];
    }
  | {
      observable: false;
      reason: 'presentation_signal_absent' | 'harness_signal_unavailable';
      occurrences: [];
      comparison: {
        status: 'unavailable';
        reason: 'presentation_unobservable';
      };
      budget: {
        status: 'unknown';
        reason: 'measurement_unavailable';
      };
    };

export interface SkillInstructionLoadOccurrence {
  ordinal: number;
  source: string;
  observedAt: string | null;
  filePath: string | null;
  memoryType: string | null;
  loadReason: string | null;
  triggerFilePath: string | null;
  parentFilePath: string | null;
}

export type SkillInstructionReachState =
  | {
      observable: true;
      occurrences: SkillInstructionLoadOccurrence[];
    }
  | {
      observable: false;
      reason:
        | 'instruction_load_signal_absent'
        | 'instrumented_no_events_received'
        | 'harness_signal_unavailable';
      occurrences: [];
    };

export type SessionExpectedRealizationState =
  | {
      status: 'associated';
      realization: SkillExpectedRealizationReference;
    }
  | {
      status: 'unavailable';
      reason: 'no_expected_realization' | 'invalid_expected_realization';
    };

export interface SessionSkillContext {
  sessionId: string;
  harness: SkillExpectedRealizationHarness;
  startedAt: string | null;
  endedAt: string | null;
  active: boolean;
  consultationClassification: SkillContextCapability;
  observations: SessionSkillContextObservation[];
  catalog: SkillCatalogPresentationState;
  expectedRealization: SessionExpectedRealizationState;
  instructions: SkillInstructionReachState;
}

export interface AnalyticsCapabilityBreakdown {
  full: number;
  summary: number;
  none: number;
  unknown: number;
}

export interface AnalyticsCoverage {
  metric_scope: 'all_sessions' | 'tool_analytics_capable';
  matching_sessions: number;
  included_sessions: number;
  excluded_sessions: number;
  fidelity_breakdown: {
    full: number;
    summary: number;
    unknown: number;
  };
  capability_breakdown: {
    history: AnalyticsCapabilityBreakdown;
    search: AnalyticsCapabilityBreakdown;
    tool_analytics: AnalyticsCapabilityBreakdown;
    live_items: AnalyticsCapabilityBreakdown;
  };
  note: string;
}

export interface HourOfWeekDataPoint {
  day_of_week: number;
  hour_of_day: number;
  session_count: number;
  message_count: number;
  user_message_count: number;
}

export interface TopSessionStat {
  id: string;
  project: string | null;
  agent: string;
  started_at: string | null;
  ended_at: string | null;
  message_count: number;
  user_message_count: number;
  tool_call_count: number;
  fidelity: string | null;
}

export interface VelocityMetrics {
  total_sessions: number;
  total_messages: number;
  total_user_messages: number;
  active_days: number;
  span_days: number;
  sessions_per_active_day: number;
  messages_per_active_day: number;
  sessions_per_calendar_day: number;
  messages_per_calendar_day: number;
  average_messages_per_session: number;
  average_user_messages_per_session: number;
  coverage: AnalyticsCoverage;
}

export interface AgentComparisonRow {
  agent: string;
  session_count: number;
  message_count: number;
  user_message_count: number;
  average_messages_per_session: number;
  full_fidelity_sessions: number;
  summary_fidelity_sessions: number;
  tool_analytics_capable_sessions: number;
  first_started_at: string | null;
  last_started_at: string | null;
}

export interface UsageSourceBreakdown {
  source: string;
  event_count: number;
  usage_event_count: number;
  session_count: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

export interface UsageCoverage {
  metric_scope: 'event_usage';
  matching_events: number;
  usage_events: number;
  missing_usage_events: number;
  matching_sessions: number;
  usage_sessions: number;
  sources_with_usage: number;
  source_breakdown: UsageSourceBreakdown[];
  note: string;
}

export interface UsageSummary {
  total_cost_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_write_tokens: number;
  total_usage_events: number;
  total_sessions: number;
  active_days: number;
  span_days: number;
  average_cost_per_active_day: number;
  average_cost_per_session: number;
  cache_hit_rate: number;
  estimated_cache_savings_usd: number;
  pricing_known_events: number;
  pricing_unknown_events: number;
  unknown_model_events: number;
  prior_total_cost_usd: number;
  cost_delta_pct: number;
  peak_day: {
    date: string | null;
    cost_usd: number;
  };
  coverage: UsageCoverage;
}

export interface UsageDailyPoint {
  date: string;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  usage_events: number;
  session_count: number;
}

export interface UsageProjectBreakdown {
  project: string;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  usage_events: number;
  session_count: number;
}

export interface UsageModelBreakdown {
  model: string;
  canonical_model: string;
  provider: string;
  family: string;
  tier: string;
  known: boolean;
  deprecated: boolean;
  pricing_status: 'known' | 'deprecated' | 'unknown';
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  usage_events: number;
  session_count: number;
}

/** One day of the date axis. `models` is empty on days with no usage. */
export interface UsageModelDailyPoint {
  date: string;
  models: UsageModelBreakdown[];
}

// ── Benchmarks (segregated bake-off comparison; the one benchmark-inclusive
// surface). See docs/specs/2026-09-02-benchmark-comparison-view-spec.md. ──

export type BenchmarkCostBasis = 'captured' | 'derived' | 'unpriced';
export type BenchmarkVerdict =
  | 'value-pick' | 'on-frontier' | 'dominated' | 'trivial-only' | 'unreliable';

/** One row in the Studies list — a single bake-off run. */
export interface BenchmarkStudySummary {
  study_id: string;              // exact per-run key (= openbench study_sha256)
  study: string;                 // display slug
  suite: string | null;          // config-level slug
  arm_count: number;
  cell_count: number;
  tasks: string[];
  date_from: string | null;
  date_to: string | null;
  total_cost_usd: number | null; // null if any arm unpriced
  cost_basis: BenchmarkCostBasis;
}

/** One arm (model + effort) aggregated across its trials. */
export interface BenchmarkArm {
  canonical_model: string;
  reasoning_effort: string | null;
  label: string;
  n: number;
  mean_score: number;
  cost_per_trial: number | null; // null if any cell unpriced
  cost_basis: BenchmarkCostBasis;
  mean_t_agent_s: number;
  cache_reads: number;
  native: boolean;
  // frontier geometry (computed):
  pareto: boolean;
  dominated_by: string | null;
  verdict: BenchmarkVerdict;
  // honesty flags:
  excluded_trials: number;
  noop_trials: number;
  token_basis: string | null;
  usage_evidence_grade: string | null;
}

export interface BenchmarkStudyDetail {
  study_id: string;
  study: string;
  suite: string | null;
  tasks: string[];
  expected_trials: number;
  arms: BenchmarkArm[];
}

/** Option lists for the five Usage filter dropdowns. */
export interface UsageFacets {
  projects: string[];
  agents: string[];
  models: string[];
  providers: string[];
  tiers: string[];
}

/** Every Usage-page panel from a single scan. Fields mirror the per-panel endpoints. */
export interface UsageOverview {
  summary: UsageSummary;
  daily: UsageDailyPoint[];
  projects: UsageProjectBreakdown[];
  models: UsageModelBreakdown[];
  models_daily: UsageModelDailyPoint[];
  tiers: UsageTierBreakdown[];
  agents: UsageAgentBreakdown[];
  top_sessions: UsageTopSessionRow[];
  coverage: UsageCoverage;
}

export interface UsageTierBreakdown {
  provider: string;
  tier: string;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  usage_events: number;
  session_count: number;
  unknown_model_events: number;
}

export interface UsageAgentBreakdown {
  agent: string;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  usage_events: number;
  session_count: number;
}

export interface UsageTopSessionRow {
  id: string;
  project: string | null;
  agent: string;
  started_at: string | null;
  ended_at: string | null;
  last_activity_at: string | null;
  message_count: number | null;
  user_message_count: number | null;
  fidelity: string | null;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  event_count: number;
  usage_events: number;
  primary_model: string;
  primary_tier: string;
  primary_provider: string;
  model_count: number;
  tier_costs: Array<{
    provider: string;
    tier: string;
    cost_usd: number;
    usage_events: number;
  }>;
  unknown_model_events: number;
  browsing_session_available: boolean;
}

export type UsageBudgetPeriod = 'day' | 'week' | 'month' | 'all_time';
export type UsageBudgetAlertState = 'ok' | 'info' | 'warning' | 'critical' | 'hard_stop_candidate';

export interface UsageBudgetThresholds {
  info: number;
  warning: number;
  critical: number;
  hard_stop_candidate: number;
}

export interface UsageBudgetFilters {
  project?: string;
  agent?: string;
  model?: string;
  provider?: string;
  tier?: string;
}

export interface UsageBudgetReport {
  name: string;
  period: UsageBudgetPeriod;
  limit_usd: number;
  spent_usd: number;
  remaining_usd: number;
  percent_used: number;
  state: UsageBudgetAlertState;
  thresholds: UsageBudgetThresholds;
  filters: UsageBudgetFilters;
  date_from: string | null;
  date_to: string | null;
  enforcing: false;
}

export interface UsageBudgetConfigStatus {
  path: string;
  present: boolean;
  valid: boolean;
  errors: string[];
}

export interface UsageBudgetsResponse {
  data: UsageBudgetReport[];
  config: UsageBudgetConfigStatus;
}

export type UsageTierFeedbackConfidence = 'low' | 'medium' | 'high';

export interface UsageTierFeedbackWindow {
  date_from: string | null;
  date_to: string | null;
  project: string | null;
  agent: string | null;
  model: string | null;
  provider: string | null;
  tier: string | null;
}

export interface UsageTierFeedbackFinding {
  kind: 'high_cost_low_tier' | 'low_complexity_premium_tier';
  recommendation: string;
  confidence: UsageTierFeedbackConfidence;
  evidence: {
    provider: string;
    tier: string;
    session_count: number;
    total_cost_usd: number;
    average_cost_usd: number;
    sample_sessions: string[];
  };
}

export interface UsageTierFeedbackCostOutlier {
  kind: 'unknown_model_spend';
  recommendation: string;
  confidence: UsageTierFeedbackConfidence;
  evidence: {
    total_cost_usd: number;
    share_of_window_cost: number;
    usage_events: number;
    sample_models: string[];
  };
}

export interface UsageTierFeedbackReport {
  generated_at: string;
  window: UsageTierFeedbackWindow;
  tier_mismatches: UsageTierFeedbackFinding[];
  cost_outliers: UsageTierFeedbackCostOutlier[];
  confidence: UsageTierFeedbackConfidence;
  evidence: {
    total_cost_usd: number;
    usage_events: number;
    session_count: number;
    method: string;
  };
  human_review_required: true;
}

export type InsightKind = 'overview' | 'workflow' | 'usage';
export type InsightProvider = 'openai' | 'anthropic' | 'gemini';

export interface InsightInputSnapshot {
  analytics_activity: ActivityDataPoint[];
  analytics_projects: ProjectBreakdown[];
  analytics_tools: ToolUsageStat[];
  analytics_hour_of_week: HourOfWeekDataPoint[];
  analytics_top_sessions: TopSessionStat[];
  analytics_velocity: VelocityMetrics;
  analytics_agents: AgentComparisonRow[];
  usage_daily: UsageDailyPoint[];
  usage_projects: UsageProjectBreakdown[];
  usage_models: UsageModelBreakdown[];
  usage_tiers: UsageTierBreakdown[];
  usage_agents: UsageAgentBreakdown[];
  usage_top_sessions: UsageTopSessionRow[];
}

export interface InsightRow {
  id: number;
  kind: InsightKind;
  title: string;
  prompt: string | null;
  content: string;
  date_from: string;
  date_to: string;
  project: string | null;
  agent: string | null;
  provider: string;
  model: string;
  analytics_summary: AnalyticsSummary;
  analytics_coverage: AnalyticsCoverage;
  usage_summary: UsageSummary;
  usage_coverage: UsageCoverage;
  input_snapshot: InsightInputSnapshot;
  created_at: string;
}

export interface InsightDbRow extends Omit<
  InsightRow,
  'analytics_summary' | 'analytics_coverage' | 'usage_summary' | 'usage_coverage' | 'input_snapshot'
> {
  analytics_summary_json: string;
  analytics_coverage_json: string;
  usage_summary_json: string;
  usage_coverage_json: string;
  input_json: string;
}

export interface SearchResultRow {
  session_id: string;
  message_id: number;
  message_ordinal: number;
  message_role: string;
  snippet: string;
  session_project: string | null;
  session_agent: string;
  session_started_at: string | null;
  session_ended_at: string | null;
  session_first_message: string | null;
}

// --- Trace quality response types ---

export interface TraceQualityAggregateMetrics {
  observation_count: number;
  error_count: number;
  total_tokens_in: number;
  total_tokens_out: number;
  total_cache_read_tokens: number;
  total_cache_write_tokens: number;
  total_cost_usd: number;
  total_duration_ms: number;
  first_observation_at: string | null;
  last_observation_at: string | null;
}

export interface TraceQualityScoreCoverage {
  scored_traces: number;
  unscored_traces: number;
  total_scores: number;
  trace_score_count: number;
  observation_score_count: number;
  numeric_score_count: number;
}

export interface TraceQualityReadCoverage {
  matching_traces: number;
  included_traces: number;
  excluded_low_coverage_traces: number;
  observations_with_usage: number;
  observations_missing_usage: number;
  score_coverage: TraceQualityScoreCoverage;
  note: string;
}

export interface TraceQualityPromptRef extends Omit<TraceQualityPromptRefRow, 'metadata_json'> {
  metadata: Record<string, unknown>;
  observation_count: number;
  trace_count: number;
}

export interface TraceQualityScore extends Omit<TraceQualityScoreRow, 'metadata_json'> {
  metadata: Record<string, unknown>;
  value: number | string | boolean | null;
}

export type TraceQualityScoreMutationValue = number | string | boolean;

export interface TraceQualityScoreCreateInput {
  target_type: 'session' | 'trace' | 'observation' | 'message' | 'event' | 'session_item';
  target_id: string;
  name: string;
  value_type: 'numeric' | 'categorical' | 'boolean' | 'text';
  value?: TraceQualityScoreMutationValue;
  numeric_value?: number;
  categorical_value?: string;
  boolean_value?: boolean | 0 | 1;
  text_value?: string;
  source?: 'human' | 'code_evaluator' | 'llm_judge' | 'api';
  evaluator_name?: string | null;
  comment?: string | null;
  metadata?: Record<string, unknown> | null;
}

export type TraceQualityScorePatchInput = Partial<TraceQualityScoreCreateInput>;

export interface TraceQualityScoreSummary {
  name: string;
  value_type: string;
  count: number;
  numeric_avg: number | null;
  numeric_min: number | null;
  numeric_max: number | null;
  boolean_true: number;
  boolean_false: number;
  categorical_values: Record<string, number>;
  scored_traces: number;
}

export type TraceQualityScoreRollupDimension = 'trace' | 'session' | 'model' | 'tool' | 'prompt' | 'day';

export interface TraceQualityScoreRollup {
  dimension: TraceQualityScoreRollupDimension;
  key: string;
  label: string | null;
  score_count: number;
  numeric_score_count: number;
  numeric_avg: number | null;
  boolean_true: number;
  boolean_false: number;
  categorical_values: Record<string, number>;
  trace_count: number;
  observation_count: number;
  first_score_at: string | null;
  last_score_at: string | null;
}

export type TraceQualityScoreRollups = Record<TraceQualityScoreRollupDimension, TraceQualityScoreRollup[]>;

export interface TraceQualityTrace extends Omit<TraceQualityTraceRow, 'metadata_json' | 'tags_json' | 'coverage_json'> {
  metadata: Record<string, unknown>;
  tags: unknown[];
  coverage: Record<string, unknown>;
  aggregate: TraceQualityAggregateMetrics;
  score_count: number;
  numeric_score_avg: number | null;
}

export interface TraceQualityTraceDetail extends TraceQualityTrace {
  prompt_refs: TraceQualityPromptRef[];
  score_summary: TraceQualityScoreSummary[];
}

export interface TraceQualityObservation extends Omit<TraceQualityObservationRow, 'metadata_json'> {
  metadata: Record<string, unknown>;
}

export interface TraceQualityObservationDetail extends TraceQualityObservation {
  trace: Pick<TraceQualityTraceRow, 'id' | 'session_id' | 'agent_type' | 'name' | 'status' | 'project' | 'started_at'>;
  prompt_refs: TraceQualityPromptRef[];
  scores: TraceQualityScore[];
}

export interface TraceQualityObservationTreeNode extends TraceQualityObservation {
  children: TraceQualityObservationTreeNode[];
}

export interface TraceQualityPromptRollup extends TraceQualityPromptRef {
  generation_count: number;
  median_duration_ms: number | null;
  total_cost_usd: number;
  total_tokens_in: number;
  total_tokens_out: number;
  score_count: number;
  median_numeric_score: number | null;
  last_seen: string | null;
}

export interface TraceQualityFindingInspection {
  /** Where the operator should drill in next. */
  target: 'traces' | 'trace' | 'observation' | 'scores' | 'usage';
  params: Record<string, string | number | boolean>;
}

export interface TraceQualityFindingWindow {
  from: string | null;
  to: string | null;
  /** e.g. 'selected_range' | 'latest_day' | 'baseline_7d'. */
  label?: string;
}

export interface TraceQualityFindingEvidence {
  metric_value?: number;
  threshold?: number;
  comparator?: 'gte' | 'lte';
  unit?: 'ratio' | 'ms' | 'usd' | 'tokens' | 'count' | 'minutes';
  window?: TraceQualityFindingWindow;
  baseline_value?: number;
  baseline_window?: TraceQualityFindingWindow;
  sample_size?: number;
  dimension?: { type: 'tool' | 'model' | 'budget' | 'score'; value: string };
  /** Capped at DEFAULT_TRACE_QUALITY_FINDING_THRESHOLDS.impacted_id_cap; impacted_total is the true count. */
  impacted_trace_ids?: string[];
  impacted_observation_ids?: string[];
  impacted_session_ids?: string[];
  impacted_total?: number;
  coverage_caveat?: string | null;
  next_inspection?: TraceQualityFindingInspection;
  [key: string]: unknown;
}

export interface TraceQualityFinding {
  id: string;
  kind: TraceQualityFindingKind;
  severity: TraceQualityFindingSeverity;
  /** null for aggregate findings that span many or no traces. */
  trace_id: string | null;
  observation_id: string | null;
  score_id: number | null;
  title: string;
  message: string;
  evidence: TraceQualityFindingEvidence;
  created_at: string | null;
}

// --- API request params ---

export interface SessionsListParams {
  limit?: number;
  cursor?: string;
  project?: string;
  agent?: string;
  date_from?: string;
  date_to?: string;
  min_messages?: number;
  max_messages?: number;
  /** Drop sessions with no browsable transcript (history capability 'none'). */
  exclude_empty?: boolean;
}

export interface MessagesListParams {
  offset?: number;
  limit?: number;
  around_ordinal?: number;
}

export interface LiveSessionsListParams {
  limit?: number;
  cursor?: string;
  project?: string;
  agent?: string;
  live_status?: string;
  fidelity?: string;
  active_only?: boolean;
}

export interface LiveItemsListParams {
  cursor?: string;
  limit?: number;
  kinds?: string[];
}

export interface SearchParams {
  q: string;
  project?: string;
  agent?: string;
  sort?: 'recent' | 'relevance';
  limit?: number;
  cursor?: string;
}

export interface AnalyticsParams {
  date_from?: string;
  date_to?: string;
  project?: string;
  agent?: string;
  model?: string;
  provider?: string;
  tier?: string;
  limit?: number;
}

export interface UsageParams {
  date_from?: string;
  date_to?: string;
  project?: string;
  agent?: string;
  model?: string;
  provider?: string;
  tier?: string;
  limit?: number;
  /**
   * Include batch-imported benchmark events (source='benchmark') in the result.
   * Off by default: benchmark runs are segregated so a bake-off does not pollute
   * real-activity cost/usage aggregates. See the 'benchmark' EventSource.
   */
  include_benchmark?: boolean;
}

export interface TraceQualityTraceListParams {
  date_from?: string;
  date_to?: string;
  project?: string;
  agent?: string;
  session_id?: string;
  status?: string;
  observation_type?: string;
  model?: string;
  tool?: string;
  tool_name?: string;
  score_name?: string;
  min_score?: number;
  max_score?: number;
  exclude_low_coverage?: boolean;
  // Findings-only: narrow derived findings by kind and/or severity.
  kind?: string;
  severity?: string;
  limit?: number;
  offset?: number;
}

export interface TraceQualityObservationListParams {
  limit?: number;
  offset?: number;
}

export interface TraceQualityScoreListParams extends TraceQualityTraceListParams {
  trace_id?: string;
  observation_id?: string;
  target_type?: string;
  target_id?: string;
  name?: string;
  source?: string;
}

export interface MonitorSessionsParams {
  status?: string;
  exclude_status?: string;
  project?: string;
  agent?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
}

export interface MonitorEventsParams {
  limit?: number;
  offset?: number;
  agent?: string;
  event_type?: string;
  tool_name?: string;
  session_id?: string;
  branch?: string;
  model?: string;
  source?: string;
  since?: string;
  until?: string;
}

export interface MonitorStatsParams {
  agent?: string;
  since?: string;
}

export interface PinsListParams {
  project?: string;
}

export interface InsightsListParams {
  date_from?: string;
  date_to?: string;
  project?: string;
  agent?: string;
  kind?: InsightKind;
  limit?: number;
}

export interface GenerateInsightParams {
  date_from: string;
  date_to: string;
  kind: InsightKind;
  project?: string;
  agent?: string;
  prompt?: string;
  provider?: InsightProvider;
  model?: string;
}
