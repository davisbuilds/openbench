import { config } from '../config.js';
import {
  createInsight,
  getAnalyticsActivity,
  getAnalyticsAgents,
  getAnalyticsCoverage,
  getAnalyticsHourOfWeek,
  getAnalyticsProjects,
  getAnalyticsSummary,
  getAnalyticsTools,
  getAnalyticsTopSessions,
  getAnalyticsVelocity,
  getUsageAgents,
  getUsageCoverage,
  getUsageDaily,
  getUsageModels,
  getUsageProjects,
  getUsageSummary,
  getUsageTiers,
  getUsageTopSessions,
} from '../db/v2-queries.js';
import type {
  GenerateInsightParams,
  InsightInputSnapshot,
  InsightKind,
  InsightProvider,
  InsightRow,
} from '../api/v2/types.js';

interface InsightDatasetPacket {
  analytics_summary: InsightRow['analytics_summary'];
  analytics_coverage: InsightRow['analytics_coverage'];
  usage_summary: InsightRow['usage_summary'];
  usage_coverage: InsightRow['usage_coverage'];
  input_snapshot: InsightInputSnapshot;
}

interface GeneratedInsightContent {
  title: string;
  content: string;
  prompt: string;
  provider: InsightProvider;
  model: string;
}

type InsightGenerator = (
  params: GenerateInsightParams,
  packet: InsightDatasetPacket,
) => Promise<GeneratedInsightContent>;

let overrideGenerator: InsightGenerator | null = null;

// Test seam for provider config (currently just the API key). config.insights is
// a startup singleton built from process.env, so a test that wants to exercise
// the "no API key" path can't rely on clearing env at runtime — it overrides the
// resolved provider config here instead of depending on the ambient shell.
type InsightProviderConfigOverride = Partial<Record<InsightProvider, { apiKey?: string | null }>>;
let providerConfigOverride: InsightProviderConfigOverride | null = null;

export function setInsightProviderConfigForTests(override: InsightProviderConfigOverride | null): void {
  providerConfigOverride = override;
}

const MAX_ACTIVITY_POINTS = 31;
const MAX_BREAKDOWN_ROWS = 8;
const MAX_TOP_SESSIONS = 8;

function kindLabel(kind: InsightKind): string {
  switch (kind) {
    case 'workflow':
      return 'Workflow Review';
    case 'usage':
      return 'Usage Review';
    default:
      return 'Overview';
  }
}

function normalizeAnalyticsAgent(agent: string | undefined): string | undefined {
  if (!agent) return undefined;
  return agent === 'claude_code' ? 'claude' : agent;
}

function normalizeUsageAgent(agent: string | undefined): string | undefined {
  if (!agent) return undefined;
  return agent === 'claude' ? 'claude_code' : agent;
}

function buildInsightDataset(params: GenerateInsightParams): InsightDatasetPacket {
  const analyticsParams = {
    date_from: params.date_from,
    date_to: params.date_to,
    project: params.project,
    agent: normalizeAnalyticsAgent(params.agent),
  };

  const usageParams = {
    date_from: params.date_from,
    date_to: params.date_to,
    project: params.project,
    agent: normalizeUsageAgent(params.agent),
  };

  return {
    analytics_summary: getAnalyticsSummary(analyticsParams),
    analytics_coverage: getAnalyticsCoverage(analyticsParams, 'all_sessions'),
    usage_summary: getUsageSummary(usageParams),
    usage_coverage: getUsageCoverage(usageParams),
    input_snapshot: {
      analytics_activity: getAnalyticsActivity(analyticsParams).slice(-MAX_ACTIVITY_POINTS),
      analytics_projects: getAnalyticsProjects(analyticsParams).slice(0, MAX_BREAKDOWN_ROWS),
      analytics_tools: getAnalyticsTools(analyticsParams).slice(0, MAX_BREAKDOWN_ROWS),
      analytics_hour_of_week: getAnalyticsHourOfWeek(analyticsParams),
      analytics_top_sessions: getAnalyticsTopSessions({ ...analyticsParams, limit: MAX_TOP_SESSIONS }),
      analytics_velocity: getAnalyticsVelocity(analyticsParams),
      analytics_agents: getAnalyticsAgents(analyticsParams).slice(0, MAX_BREAKDOWN_ROWS),
      usage_daily: getUsageDaily(usageParams).slice(-MAX_ACTIVITY_POINTS),
      usage_projects: getUsageProjects(usageParams).slice(0, MAX_BREAKDOWN_ROWS),
      usage_models: getUsageModels(usageParams).slice(0, MAX_BREAKDOWN_ROWS),
      usage_tiers: getUsageTiers(usageParams).slice(0, MAX_BREAKDOWN_ROWS),
      usage_agents: getUsageAgents(usageParams).slice(0, MAX_BREAKDOWN_ROWS),
      usage_top_sessions: getUsageTopSessions({ ...usageParams, limit: MAX_TOP_SESSIONS }),
    },
  };
}

function buildSystemInstructions(kind: InsightKind): string {
  switch (kind) {
    case 'workflow':
      return [
        'You are generating an operational workflow review for AgentMonitor.',
        'Focus on process bottlenecks, agent/tool usage patterns, and concrete workflow improvements.',
        'Stay grounded in the provided data. If coverage is partial, say so explicitly.',
      ].join(' ');
    case 'usage':
      return [
        'You are generating a usage and cost review for AgentMonitor.',
        'Focus on spend concentration, token patterns, model mix, and pragmatic efficiency recommendations.',
        'Stay grounded in the provided data. If coverage is partial, say so explicitly.',
      ].join(' ');
    default:
      return [
        'You are generating a concise operational summary for AgentMonitor.',
        'Focus on delivery patterns, throughput, project concentration, and notable trends.',
        'Stay grounded in the provided data. If coverage is partial, say so explicitly.',
      ].join(' ');
  }
}

function buildPrompt(params: GenerateInsightParams, packet: InsightDatasetPacket): string {
  const scopeLines = [
    `Kind: ${kindLabel(params.kind)}`,
    `Date range: ${params.date_from} to ${params.date_to}`,
    `Project filter: ${params.project || 'all projects'}`,
    `Agent filter: ${params.agent || 'all agents'}`,
  ];

  const instructions = [
    'Return markdown.',
    `The first line must be a level-1 heading with a short title for this ${kindLabel(params.kind).toLowerCase()}.`,
    'Include sections named: Scope, Findings, Recommendations.',
    'Keep Findings and Recommendations concrete and evidence-based.',
    'Mention data-coverage limits directly in Scope.',
    'Do not fabricate missing sessions, tools, models, or costs.',
  ];

  if (params.prompt?.trim()) {
    instructions.push(`Additional user steering: ${params.prompt.trim()}`);
  }

  return [
    '## Scope',
    ...scopeLines.map(line => `- ${line}`),
    '',
    '## Output Requirements',
    ...instructions.map(line => `- ${line}`),
    '',
    '## Dataset',
    '```json',
    JSON.stringify(packet, null, 2),
    '```',
  ].join('\n');
}

function extractOutputText(payload: unknown): string {
  if (!payload || typeof payload !== 'object') {
    return '';
  }

  const record = payload as {
    output_text?: unknown;
    output?: Array<{ type?: string; content?: Array<{ type?: string; text?: string }> }>;
  };

  if (typeof record.output_text === 'string' && record.output_text.trim()) {
    return record.output_text.trim();
  }

  if (!Array.isArray(record.output)) {
    return '';
  }

  const parts: string[] = [];
  for (const item of record.output) {
    if (item?.type !== 'message' || !Array.isArray(item.content)) continue;
    for (const content of item.content) {
      if (content?.type === 'output_text' && typeof content.text === 'string') {
        parts.push(content.text);
      }
    }
  }

  return parts.join('\n').trim();
}

function extractTitle(content: string, kind: InsightKind, params: GenerateInsightParams): string {
  const heading = content.match(/^#\s+(.+)$/m)?.[1]?.trim();
  if (heading) return heading;

  const range = params.date_from === params.date_to
    ? params.date_from
    : `${params.date_from} to ${params.date_to}`;
  return `${kindLabel(kind)} • ${range}`;
}

function getProviderConfig(provider: InsightProvider): {
  provider: InsightProvider;
  apiKey: string | null;
  model: string;
  baseUrl: string;
} {
  const resolved = config.insights.providers[provider];
  const override = providerConfigOverride?.[provider];
  return {
    provider,
    apiKey: override && 'apiKey' in override ? override.apiKey ?? null : resolved.apiKey,
    model: resolved.model,
    baseUrl: resolved.baseUrl,
  };
}

function resolveRequestedProvider(params: GenerateInsightParams): {
  provider: InsightProvider;
  apiKey: string | null;
  model: string;
  baseUrl: string;
} {
  const provider = params.provider ?? config.insights.provider;
  const configForProvider = getProviderConfig(provider);
  return {
    ...configForProvider,
    model: params.model?.trim() || configForProvider.model,
  };
}

async function generateWithOpenAI(
  params: GenerateInsightParams,
  packet: InsightDatasetPacket,
): Promise<GeneratedInsightContent> {
  const provider = resolveRequestedProvider(params);
  if (!provider.apiKey) {
    throw new Error('Insight generation requires AGENTMONITOR_OPENAI_API_KEY or OPENAI_API_KEY.');
  }

  const prompt = buildPrompt(params, packet);
  const response = await fetch(`${provider.baseUrl}/responses`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${provider.apiKey}`,
    },
    body: JSON.stringify({
      model: provider.model,
      input: [
        {
          role: 'developer',
          content: buildSystemInstructions(params.kind),
        },
        {
          role: 'user',
          content: prompt,
        },
      ],
      max_output_tokens: 1800,
    }),
  });

  if (!response.ok) {
    const body = await response.text().catch(() => '');
    throw new Error(`Insight generation failed (${response.status}): ${body}`);
  }

  const payload = await response.json();
  const content = extractOutputText(payload);
  if (!content) {
    throw new Error('Insight generation returned no text output.');
  }

  return {
    title: extractTitle(content, params.kind, params),
    content,
    prompt,
    provider: 'openai',
    model: provider.model,
  };
}

function extractAnthropicText(payload: unknown): string {
  if (!payload || typeof payload !== 'object') return '';
  const record = payload as {
    content?: Array<{ type?: string; text?: string }>;
  };
  if (!Array.isArray(record.content)) return '';
  return record.content
    .filter(item => item?.type === 'text' && typeof item.text === 'string')
    .map(item => item.text as string)
    .join('\n')
    .trim();
}

async function generateWithAnthropic(
  params: GenerateInsightParams,
  packet: InsightDatasetPacket,
): Promise<GeneratedInsightContent> {
  const provider = resolveRequestedProvider(params);
  if (!provider.apiKey) {
    throw new Error('Insight generation requires AGENTMONITOR_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY.');
  }

  const prompt = buildPrompt(params, packet);
  const response = await fetch(`${provider.baseUrl}/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': provider.apiKey,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: provider.model,
      max_tokens: 1800,
      system: buildSystemInstructions(params.kind),
      messages: [
        {
          role: 'user',
          content: prompt,
        },
      ],
    }),
  });

  if (!response.ok) {
    const body = await response.text().catch(() => '');
    throw new Error(`Insight generation failed (${response.status}): ${body}`);
  }

  const payload = await response.json();
  const content = extractAnthropicText(payload);
  if (!content) {
    throw new Error('Insight generation returned no text output.');
  }

  return {
    title: extractTitle(content, params.kind, params),
    content,
    prompt,
    provider: 'anthropic',
    model: provider.model,
  };
}

function extractGeminiText(payload: unknown): string {
  if (!payload || typeof payload !== 'object') return '';
  const record = payload as {
    candidates?: Array<{
      content?: {
        parts?: Array<{ text?: string }>;
      };
    }>;
  };

  const parts = record.candidates?.[0]?.content?.parts;
  if (!Array.isArray(parts)) return '';
  return parts
    .map(part => typeof part.text === 'string' ? part.text : '')
    .filter(Boolean)
    .join('\n')
    .trim();
}

function normalizeGeminiModel(model: string): string {
  return model.replace(/^models\//, '');
}

async function generateWithGemini(
  params: GenerateInsightParams,
  packet: InsightDatasetPacket,
): Promise<GeneratedInsightContent> {
  const provider = resolveRequestedProvider(params);
  if (!provider.apiKey) {
    throw new Error('Insight generation requires AGENTMONITOR_GEMINI_API_KEY, GEMINI_API_KEY, or GOOGLE_API_KEY.');
  }

  const prompt = buildPrompt(params, packet);
  const model = normalizeGeminiModel(provider.model);
  const response = await fetch(`${provider.baseUrl}/models/${model}:generateContent`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-goog-api-key': provider.apiKey,
      'x-goog-api-client': 'agentmonitor-insights/1.0',
    },
    body: JSON.stringify({
      systemInstruction: {
        parts: [{ text: buildSystemInstructions(params.kind) }],
      },
      contents: [
        {
          role: 'user',
          parts: [{ text: prompt }],
        },
      ],
      generationConfig: {
        maxOutputTokens: 1800,
      },
    }),
  });

  if (!response.ok) {
    const body = await response.text().catch(() => '');
    throw new Error(`Insight generation failed (${response.status}): ${body}`);
  }

  const payload = await response.json();
  const content = extractGeminiText(payload);
  if (!content) {
    throw new Error('Insight generation returned no text output.');
  }

  return {
    title: extractTitle(content, params.kind, params),
    content,
    prompt,
    provider: 'gemini',
    model,
  };
}

async function generateWithProvider(
  params: GenerateInsightParams,
  packet: InsightDatasetPacket,
): Promise<GeneratedInsightContent> {
  const provider = params.provider ?? config.insights.provider;
  switch (provider) {
    case 'anthropic':
      return generateWithAnthropic(params, packet);
    case 'gemini':
      return generateWithGemini(params, packet);
    default:
      return generateWithOpenAI(params, packet);
  }
}

export function setInsightGeneratorForTests(generator: InsightGenerator | null): void {
  overrideGenerator = generator;
}

export async function generateInsight(params: GenerateInsightParams): Promise<InsightRow> {
  if (!params.date_from || !params.date_to) {
    throw new Error('date_from and date_to are required.');
  }
  if (params.date_from > params.date_to) {
    throw new Error('date_from must be on or before date_to.');
  }

  const dataset = buildInsightDataset(params);
  const generator = overrideGenerator ?? generateWithProvider;
  const generated = await generator(params, dataset);

  return createInsight({
    kind: params.kind,
    title: generated.title,
    prompt: params.prompt?.trim() || null,
    content: generated.content,
    date_from: params.date_from,
    date_to: params.date_to,
    project: params.project || null,
    agent: params.agent || null,
    provider: generated.provider,
    model: generated.model,
    analytics_summary: dataset.analytics_summary,
    analytics_coverage: dataset.analytics_coverage,
    usage_summary: dataset.usage_summary,
    usage_coverage: dataset.usage_coverage,
    input_snapshot: dataset.input_snapshot,
  });
}
