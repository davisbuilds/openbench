import { getUsageModels, getUsageSummary, getUsageTopSessions } from '../db/v2-queries.js';
import type {
  UsageModelBreakdown,
  UsageParams,
  UsageTierFeedbackConfidence,
  UsageTierFeedbackCostOutlier,
  UsageTierFeedbackFinding,
  UsageTierFeedbackReport,
  UsageTierFeedbackWindow,
  UsageTopSessionRow,
} from '../api/v2/types.js';

const LOW_TIERS = new Set(['economy', 'haiku', 'flash']);
const PREMIUM_TIERS = new Set(['premium', 'opus', 'pro', 'ultra']);
const HIGH_COST_LOW_TIER_MIN_SESSIONS = 2;
const HIGH_COST_LOW_TIER_MIN_SESSION_COST = 0.05;
const LOW_COMPLEXITY_PREMIUM_MIN_SESSIONS = 2;
const LOW_COMPLEXITY_PREMIUM_MAX_SESSION_COST = 0.01;
const LOW_COMPLEXITY_PREMIUM_MAX_TOKENS = 500;
const LOW_COMPLEXITY_PREMIUM_MAX_MESSAGES = 4;
const LOW_COMPLEXITY_PREMIUM_MAX_DURATION_MINUTES = 15;
const UNKNOWN_MODEL_MIN_COST_USD = 0.05;
const UNKNOWN_MODEL_MIN_SHARE = 0.5;

function roundCost(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function roundRate(value: number): number {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function windowFromParams(params: UsageParams): UsageTierFeedbackWindow {
  return {
    date_from: params.date_from ?? null,
    date_to: params.date_to ?? null,
    project: params.project ?? null,
    agent: params.agent ?? null,
    model: params.model ?? null,
    provider: params.provider ?? null,
    tier: params.tier ?? null,
  };
}

function durationMinutes(session: UsageTopSessionRow): number | null {
  if (!session.started_at || !session.ended_at) return null;
  const started = new Date(session.started_at).getTime();
  const ended = new Date(session.ended_at).getTime();
  if (Number.isNaN(started) || Number.isNaN(ended) || ended < started) return null;
  return (ended - started) / 60_000;
}

function isLowComplexityPremiumSession(session: UsageTopSessionRow): boolean {
  if (!PREMIUM_TIERS.has(session.primary_tier)) return false;
  if (session.cost_usd > LOW_COMPLEXITY_PREMIUM_MAX_SESSION_COST) return false;
  if ((session.input_tokens + session.output_tokens) > LOW_COMPLEXITY_PREMIUM_MAX_TOKENS) return false;
  if (session.message_count != null && session.message_count > LOW_COMPLEXITY_PREMIUM_MAX_MESSAGES) return false;
  const duration = durationMinutes(session);
  if (duration != null && duration > LOW_COMPLEXITY_PREMIUM_MAX_DURATION_MINUTES) return false;
  return true;
}

function groupSessionsByProviderTier(sessions: UsageTopSessionRow[]): Map<string, UsageTopSessionRow[]> {
  const groups = new Map<string, UsageTopSessionRow[]>();
  for (const session of sessions) {
    const key = `${session.primary_provider}\0${session.primary_tier}`;
    const existing = groups.get(key) ?? [];
    existing.push(session);
    groups.set(key, existing);
  }
  return groups;
}

function sampleSessionIds(sessions: UsageTopSessionRow[]): string[] {
  return [...sessions]
    .sort((a, b) => b.cost_usd - a.cost_usd || b.input_tokens - a.input_tokens || a.id.localeCompare(b.id))
    .slice(0, 5)
    .map(session => session.id);
}

function providerTierEvidence(sessions: UsageTopSessionRow[]): UsageTierFeedbackFinding['evidence'] {
  const totalCost = roundCost(sessions.reduce((sum, session) => sum + session.cost_usd, 0));
  return {
    provider: sessions[0]?.primary_provider ?? 'unknown',
    tier: sessions[0]?.primary_tier ?? 'unknown',
    session_count: sessions.length,
    total_cost_usd: totalCost,
    average_cost_usd: sessions.length > 0 ? roundCost(totalCost / sessions.length) : 0,
    sample_sessions: sampleSessionIds(sessions),
  };
}

function highCostLowTierFindings(sessions: UsageTopSessionRow[]): UsageTierFeedbackFinding[] {
  const candidates = sessions.filter(session => LOW_TIERS.has(session.primary_tier) && session.cost_usd >= HIGH_COST_LOW_TIER_MIN_SESSION_COST);
  const findings: UsageTierFeedbackFinding[] = [];

  for (const group of groupSessionsByProviderTier(candidates).values()) {
    if (group.length < HIGH_COST_LOW_TIER_MIN_SESSIONS) continue;
    findings.push({
      kind: 'high_cost_low_tier',
      recommendation: 'Review whether repeated high-cost sessions should use a standard or premium tier.',
      confidence: 'medium',
      evidence: providerTierEvidence(group),
    });
  }

  return findings;
}

function lowComplexityPremiumFindings(sessions: UsageTopSessionRow[]): UsageTierFeedbackFinding[] {
  const candidates = sessions.filter(isLowComplexityPremiumSession);
  const findings: UsageTierFeedbackFinding[] = [];

  for (const group of groupSessionsByProviderTier(candidates).values()) {
    if (group.length < LOW_COMPLEXITY_PREMIUM_MIN_SESSIONS) continue;
    findings.push({
      kind: 'low_complexity_premium_tier',
      recommendation: 'Review whether repeated low-complexity sessions can move to a lower tier.',
      confidence: 'low',
      evidence: providerTierEvidence(group),
    });
  }

  return findings;
}

function unknownModelSpendOutliers(models: UsageModelBreakdown[], totalCostUsd: number): UsageTierFeedbackCostOutlier[] {
  if (totalCostUsd <= 0) return [];
  const unknownRows = models.filter(model => model.pricing_status === 'unknown' && model.cost_usd > 0);
  const unknownCost = roundCost(unknownRows.reduce((sum, model) => sum + model.cost_usd, 0));
  const share = roundRate(unknownCost / totalCostUsd);
  if (unknownCost < UNKNOWN_MODEL_MIN_COST_USD || share < UNKNOWN_MODEL_MIN_SHARE) return [];

  return [{
    kind: 'unknown_model_spend',
    recommendation: 'Review unknown models that dominate spend before trusting tier or pricing conclusions.',
    confidence: 'medium',
    evidence: {
      total_cost_usd: unknownCost,
      share_of_window_cost: share,
      usage_events: unknownRows.reduce((sum, model) => sum + model.usage_events, 0),
      sample_models: unknownRows
        .sort((a, b) => b.cost_usd - a.cost_usd || a.model.localeCompare(b.model))
        .slice(0, 5)
        .map(model => model.model),
    },
  }];
}

function overallConfidence(
  tierFindings: UsageTierFeedbackFinding[],
  outliers: UsageTierFeedbackCostOutlier[],
): UsageTierFeedbackConfidence {
  const confidences = [...tierFindings.map(finding => finding.confidence), ...outliers.map(outlier => outlier.confidence)];
  if (confidences.includes('high')) return 'high';
  if (confidences.includes('medium')) return 'medium';
  return 'low';
}

export function getUsageTierFeedback(params: UsageParams = {}): UsageTierFeedbackReport {
  const summary = getUsageSummary(params);
  const sessions = getUsageTopSessions({ ...params, limit: 50 });
  const models = getUsageModels(params);
  const tierMismatches = [
    ...highCostLowTierFindings(sessions),
    ...lowComplexityPremiumFindings(sessions),
  ].sort((a, b) => (
    b.evidence.total_cost_usd - a.evidence.total_cost_usd
    || b.evidence.session_count - a.evidence.session_count
    || a.kind.localeCompare(b.kind)
    || a.evidence.provider.localeCompare(b.evidence.provider)
    || a.evidence.tier.localeCompare(b.evidence.tier)
  ));
  const costOutliers = unknownModelSpendOutliers(models, summary.total_cost_usd);

  return {
    generated_at: new Date().toISOString(),
    window: windowFromParams(params),
    tier_mismatches: tierMismatches,
    cost_outliers: costOutliers,
    confidence: overallConfidence(tierMismatches, costOutliers),
    evidence: {
      total_cost_usd: summary.total_cost_usd,
      usage_events: summary.total_usage_events,
      session_count: summary.total_sessions,
      method: 'event_usage_summary_and_top_sessions_without_message_content',
    },
    human_review_required: true,
  };
}
