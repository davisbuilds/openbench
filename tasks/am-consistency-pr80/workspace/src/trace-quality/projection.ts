import { createHash } from 'node:crypto';
import type {
  TraceQualityCoverage,
  TraceQualityObservationRow,
  TraceQualityObservationType,
  TraceQualityPayloadPolicy,
  TraceQualitySeverity,
  TraceQualityTraceRow,
} from './types.js';

// Stamped into projected trace metadata. Internal-only since the reframe (the
// projection is no longer persisted, so nothing outside this module needs it).
const TRACE_QUALITY_PROJECTION_VERSION = 'trace-quality:v1';

export interface EventProjectionSource {
  id: number;
  event_id: string | null;
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

export interface BrowsingSessionProjectionSource {
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
  capabilities_json: string | null;
  file_path: string | null;
  file_size: number | null;
  file_hash: string | null;
}

export interface SessionTurnProjectionSource {
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

export interface SessionItemProjectionSource {
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

export interface MessageProjectionSource {
  id: number;
  session_id: string;
  ordinal: number;
  role: string;
  content: string;
  timestamp: string | null;
  has_thinking: number;
  has_tool_use: number;
  content_length: number;
}

export interface ToolCallProjectionSource {
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

export interface TraceQualityProjectionInput {
  sessionId: string;
  agentType?: string | null;
  project?: string | null;
  branch?: string | null;
  browsingSession?: BrowsingSessionProjectionSource | null;
  events?: readonly EventProjectionSource[];
  turns?: readonly SessionTurnProjectionSource[];
  sessionItems?: readonly SessionItemProjectionSource[];
  messages?: readonly MessageProjectionSource[];
  toolCalls?: readonly ToolCallProjectionSource[];
}

export type ProjectedTraceQualityTrace = Omit<TraceQualityTraceRow, 'created_at'>;
export type ProjectedTraceQualityObservation = Omit<TraceQualityObservationRow, 'created_at'>;

export interface TraceQualityProjectionResult {
  traces: ProjectedTraceQualityTrace[];
  observations: ProjectedTraceQualityObservation[];
  warnings: string[];
}

interface ObservationBuildInput {
  id: string;
  traceId: string;
  parentObservationId?: string | null;
  sessionId: string;
  sourceKind: ProjectedTraceQualityObservation['source_kind'];
  sourceId: string | null;
  sourceItemId?: string | null;
  observationType: TraceQualityObservationType;
  name: string;
  status?: string | null;
  statusMessage?: string | null;
  severity?: TraceQualitySeverity | string | null;
  model?: string | null;
  toolName?: string | null;
  startedAt?: string | null;
  endedAt?: string | null;
  durationMs?: number | null;
  tokensIn?: number;
  tokensOut?: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
  costUsd?: number | null;
  inputHash?: string | null;
  outputHash?: string | null;
  inputSummary?: string | null;
  outputSummary?: string | null;
  payloadPolicy: TraceQualityPayloadPolicy;
  metadata?: Record<string, unknown>;
}

function stableHash(value: unknown): string {
  return createHash('sha256')
    .update(JSON.stringify(value) ?? 'null')
    .digest('hex');
}

function stableId(prefix: string, parts: readonly unknown[]): string {
  return `${prefix}-${stableHash([TRACE_QUALITY_PROJECTION_VERSION, ...parts]).slice(0, 24)}`;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function parseJsonRecord(value: string | null | undefined): Record<string, unknown> | undefined {
  if (!value) return undefined;
  try {
    return asRecord(JSON.parse(value));
  } catch {
    return undefined;
  }
}

function parseJsonValue(value: string | null | undefined): unknown {
  if (!value) return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function serializeJson(value: Record<string, unknown> | unknown[]): string {
  return JSON.stringify(value);
}

function summarizeText(value: string | null | undefined, maxLength = 240): string | null {
  const trimmed = value?.replace(/\s+/g, ' ').trim();
  if (!trimmed) return null;
  return trimmed.length > maxLength ? `${trimmed.slice(0, maxLength - 3)}...` : trimmed;
}

function stringifySummaryValue(value: unknown): string | null {
  if (typeof value === 'string') return summarizeText(value);
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return null;
  const record = asRecord(value);
  if (record) return summarizeRecord(record);
  try {
    return summarizeText(JSON.stringify(value));
  } catch {
    return null;
  }
}

function summarizeRecord(record: Record<string, unknown>): string | null {
  const priorityKeys = [
    'text',
    'content',
    'summary',
    'content_preview',
    'response_preview',
    'message',
    'error',
    'command',
    'file_path',
    'path',
    'query',
    'pattern',
    'tool_name',
    'name',
    'status',
  ];
  const parts: string[] = [];

  for (const key of priorityKeys) {
    if (!Object.prototype.hasOwnProperty.call(record, key)) continue;
    const value = stringifySummaryValue(record[key]);
    if (!value) continue;
    if (key === 'text' || key === 'content' || key === 'summary' || key.endsWith('_preview')) {
      return value;
    }
    parts.push(`${key}: ${value}`);
    if (parts.length >= 3) break;
  }

  return parts.length > 0 ? parts.join(', ') : null;
}

function extractContentText(value: string): string | null {
  const parsed = parseJsonValue(value);
  if (typeof parsed === 'string') return summarizeText(parsed);
  if (!Array.isArray(parsed)) return null;

  const parts = parsed.flatMap(entry => {
    const block = asRecord(entry);
    if (!block) return [];
    const text = typeof block.text === 'string'
      ? block.text
      : typeof block.content === 'string'
        ? block.content
        : typeof block.summary === 'string'
          ? block.summary
          : null;
    return text ? [text] : [];
  });

  return summarizeText(parts.join('\n'));
}

function eventTimestamp(event: EventProjectionSource): string | null {
  return event.client_timestamp ?? event.created_at ?? null;
}

function addDuration(start: string | null, durationMs: number | null | undefined): string | null {
  if (!start || durationMs == null) return start;
  const startMs = Date.parse(start);
  if (Number.isNaN(startMs)) return start;
  return new Date(startMs + durationMs).toISOString();
}

function durationBetween(start: string | null, end: string | null): number | null {
  if (!start || !end) return null;
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  if (Number.isNaN(startMs) || Number.isNaN(endMs) || endMs < startMs) return null;
  return endMs - startMs;
}

function minIso(values: Array<string | null | undefined>): string | null {
  const filtered = values.filter((value): value is string => Boolean(value));
  if (filtered.length === 0) return null;
  return filtered.sort()[0];
}

function maxIso(values: Array<string | null | undefined>): string | null {
  const filtered = values.filter((value): value is string => Boolean(value));
  if (filtered.length === 0) return null;
  return filtered.sort().at(-1) ?? null;
}

function confidenceForBrowsingSession(session: BrowsingSessionProjectionSource | null | undefined): TraceQualityCoverage['projection_confidence'] {
  if (session?.fidelity === 'full' || session?.integration_mode === 'claude-jsonl') return 'high';
  if (session?.fidelity === 'summary' || session?.integration_mode) return 'medium';
  return 'unknown';
}

function buildTrace(input: {
  id: string;
  sessionId: string;
  browsingSessionId?: string | null;
  sourceTraceId?: string | null;
  agentType: string;
  name: string;
  status?: string | null;
  project?: string | null;
  branch?: string | null;
  startedAt?: string | null;
  endedAt?: string | null;
  durationMs?: number | null;
  metadata?: Record<string, unknown>;
  tags?: string[];
  coverage: TraceQualityCoverage;
}): ProjectedTraceQualityTrace {
  const durationMs = input.durationMs ?? durationBetween(input.startedAt ?? null, input.endedAt ?? null);
  return {
    id: input.id,
    session_id: input.sessionId,
    browsing_session_id: input.browsingSessionId ?? null,
    source_trace_id: input.sourceTraceId ?? null,
    agent_type: input.agentType,
    name: input.name,
    status: input.status ?? null,
    project: input.project ?? null,
    branch: input.branch ?? null,
    started_at: input.startedAt ?? null,
    ended_at: input.endedAt ?? null,
    duration_ms: durationMs,
    metadata_json: serializeJson({
      projection_version: TRACE_QUALITY_PROJECTION_VERSION,
      ...(input.metadata ?? {}),
    }),
    tags_json: serializeJson(input.tags ?? []),
    coverage_json: serializeJson(input.coverage),
  };
}

function buildObservation(input: ObservationBuildInput): ProjectedTraceQualityObservation {
  return {
    id: input.id,
    trace_id: input.traceId,
    parent_observation_id: input.parentObservationId ?? null,
    session_id: input.sessionId,
    source_kind: input.sourceKind,
    source_id: input.sourceId,
    source_item_id: input.sourceItemId ?? null,
    observation_type: input.observationType,
    name: input.name,
    status: input.status ?? null,
    status_message: input.statusMessage ?? null,
    severity: input.severity ?? null,
    model: input.model ?? null,
    tool_name: input.toolName ?? null,
    started_at: input.startedAt ?? null,
    ended_at: input.endedAt ?? null,
    duration_ms: input.durationMs ?? durationBetween(input.startedAt ?? null, input.endedAt ?? null),
    tokens_in: input.tokensIn ?? 0,
    tokens_out: input.tokensOut ?? 0,
    cache_read_tokens: input.cacheReadTokens ?? 0,
    cache_write_tokens: input.cacheWriteTokens ?? 0,
    cost_usd: input.costUsd ?? null,
    input_hash: input.inputHash ?? null,
    output_hash: input.outputHash ?? null,
    input_summary: input.inputSummary ?? null,
    output_summary: input.outputSummary ?? null,
    payload_policy: input.payloadPolicy,
    metadata_json: serializeJson({
      projection_version: TRACE_QUALITY_PROJECTION_VERSION,
      ...(input.metadata ?? {}),
    }),
  };
}

function normalizeAgentType(input: TraceQualityProjectionInput): string {
  return input.agentType
    ?? input.browsingSession?.agent
    ?? input.events?.[0]?.agent_type
    ?? input.turns?.[0]?.agent_type
    ?? 'unknown';
}

function normalizeProject(input: TraceQualityProjectionInput): string | null {
  return input.project
    ?? input.browsingSession?.project
    ?? input.events?.find(event => event.project)?.project
    ?? null;
}

function normalizeBranch(input: TraceQualityProjectionInput): string | null {
  return input.branch
    ?? input.events?.find(event => event.branch)?.branch
    ?? null;
}

function eventObservationType(event: EventProjectionSource): TraceQualityObservationType {
  if (event.event_type === 'tool_use') return 'tool';
  if (event.event_type === 'response' || event.event_type === 'llm_response' || event.event_type === 'llm_request') return 'generation';
  return 'event';
}

function itemObservationType(item: SessionItemProjectionSource): TraceQualityObservationType {
  switch (item.kind) {
    case 'assistant_message':
      return 'generation';
    case 'reasoning':
      return 'span';
    case 'tool_call':
    case 'tool_result':
    case 'command_execution':
      return 'tool';
    default:
      return 'event';
  }
}

function messageObservationType(message: MessageProjectionSource): TraceQualityObservationType {
  return message.role === 'assistant' ? 'generation' : 'event';
}

function severityFromStatus(status: string | null | undefined): TraceQualitySeverity {
  if (status === 'error') return 'error';
  if (status === 'timeout') return 'warning';
  return 'info';
}

function isRedactedPayload(payload: Record<string, unknown> | undefined): boolean {
  if (!payload) return false;
  if (payload.redacted === true || payload.input_redacted === true) return true;
  if (typeof payload.reason === 'string' && payload.reason.includes('capture_disabled')) return true;
  const input = asRecord(payload.input);
  return input?.redacted === true;
}

function payloadPolicyForPayload(payload: Record<string, unknown> | undefined): TraceQualityPayloadPolicy {
  return isRedactedPayload(payload) ? 'hash_only' : 'source_ref';
}

function eventPayloadPolicy(event: EventProjectionSource): TraceQualityPayloadPolicy {
  return event.payload_truncated ? 'hash_only' : 'summary_only';
}

function eventName(event: EventProjectionSource): string {
  switch (event.event_type) {
    case 'tool_use':
      return event.tool_name ? `Tool: ${event.tool_name}` : 'Tool call';
    case 'llm_response':
    case 'response':
      return event.model ? `LLM response: ${event.model}` : 'LLM response';
    case 'llm_request':
      return event.model ? `LLM request: ${event.model}` : 'LLM request';
    case 'session_start':
      return 'Session start';
    case 'session_end':
      return 'Session end';
    case 'error':
      return 'Error';
    default:
      return event.event_type.replaceAll('_', ' ');
  }
}

function itemName(item: SessionItemProjectionSource, payload: Record<string, unknown> | undefined): string {
  switch (item.kind) {
    case 'tool_call':
    case 'command_execution':
      return typeof payload?.tool_name === 'string'
        ? `Tool: ${payload.tool_name}`
        : typeof payload?.name === 'string'
          ? `Tool: ${payload.name}`
          : 'Tool call';
    case 'tool_result':
      return 'Tool result';
    case 'assistant_message':
      return 'Assistant message';
    case 'user_message':
      return 'User message';
    case 'reasoning':
      return 'Reasoning';
    default:
      return item.kind.replaceAll('_', ' ');
  }
}

function eventStatusMessage(event: EventProjectionSource, metadata: Record<string, unknown> | undefined): string | null {
  if (typeof metadata?.error === 'string') return summarizeText(metadata.error);
  if (typeof metadata?.message === 'string' && (event.status === 'error' || event.event_type === 'error')) {
    return summarizeText(metadata.message);
  }
  return null;
}

function payloadStatusMessage(payload: Record<string, unknown> | undefined, status: string | null | undefined): string | null {
  if (!payload || status !== 'error') return null;
  if (typeof payload.error === 'string') return summarizeText(payload.error);
  if (typeof payload.message === 'string') return summarizeText(payload.message);
  return null;
}

function eventInputSummary(event: EventProjectionSource, metadata: Record<string, unknown> | undefined): string | null {
  if (!metadata) return null;
  if (event.event_type === 'tool_use') {
    return summarizeRecord(metadata);
  }
  if (event.event_type === 'user_prompt') {
    return summarizeRecord(metadata);
  }
  return null;
}

function eventOutputSummary(event: EventProjectionSource, metadata: Record<string, unknown> | undefined): string | null {
  if (!metadata) return null;
  if (event.event_type === 'response' || event.event_type === 'llm_response') {
    return summarizeRecord(metadata);
  }
  if (event.event_type === 'error' || event.status === 'error') {
    return eventStatusMessage(event, metadata);
  }
  return null;
}

function itemInputSummary(item: SessionItemProjectionSource, payload: Record<string, unknown> | undefined): string | null {
  if (!payload) return null;
  if (item.kind === 'user_message') return summarizeRecord(payload);
  if (item.kind === 'tool_call' || item.kind === 'command_execution') {
    const input = asRecord(payload.input);
    if (input) return summarizeRecord(input);
    return summarizeRecord(payload);
  }
  return null;
}

function itemOutputSummary(item: SessionItemProjectionSource, payload: Record<string, unknown> | undefined): string | null {
  if (!payload) return null;
  if (item.kind === 'assistant_message' || item.kind === 'reasoning' || item.kind === 'tool_result') {
    return summarizeRecord(payload);
  }
  return null;
}

function toolNameFromPayload(payload: Record<string, unknown> | undefined): string | null {
  if (typeof payload?.tool_name === 'string') return payload.tool_name;
  if (typeof payload?.name === 'string') return payload.name;
  if (typeof payload?.command === 'string') return 'command';
  return null;
}

function projectEventObservation(traceId: string, event: EventProjectionSource): ProjectedTraceQualityObservation {
  const metadata = parseJsonRecord(event.metadata);
  const startedAt = eventTimestamp(event);
  const endedAt = addDuration(startedAt, event.duration_ms);
  const metadataHash = stableHash({
    source: 'events',
    id: event.id,
    metadata: event.metadata,
  });

  return buildObservation({
    id: stableId('tq-obs', ['event', event.id]),
    traceId,
    sessionId: event.session_id,
    sourceKind: 'event',
    sourceId: String(event.id),
    sourceItemId: event.event_id,
    observationType: eventObservationType(event),
    name: eventName(event),
    status: event.status,
    statusMessage: eventStatusMessage(event, metadata),
    severity: event.event_type === 'error' ? 'error' : severityFromStatus(event.status),
    model: event.model,
    toolName: event.tool_name,
    startedAt,
    endedAt,
    durationMs: event.duration_ms,
    tokensIn: event.tokens_in,
    tokensOut: event.tokens_out,
    cacheReadTokens: event.cache_read_tokens,
    cacheWriteTokens: event.cache_write_tokens,
    costUsd: event.cost_usd,
    inputHash: metadataHash,
    outputHash: eventOutputSummary(event, metadata) ? metadataHash : null,
    inputSummary: eventInputSummary(event, metadata),
    outputSummary: eventOutputSummary(event, metadata),
    payloadPolicy: eventPayloadPolicy(event),
    metadata: {
      source_table: 'events',
      event_id: event.event_id,
      event_type: event.event_type,
      event_source: event.source,
      payload_truncated: event.payload_truncated === 1,
    },
  });
}

function projectSessionItemObservation(
  traceId: string,
  item: SessionItemProjectionSource,
  parentObservationId: string | null,
): ProjectedTraceQualityObservation {
  const payload = parseJsonRecord(item.payload_json);
  const payloadHash = stableHash({
    source: 'session_items',
    id: item.id,
    payload: item.payload_json,
  });
  const inputSummary = itemInputSummary(item, payload);
  const outputSummary = itemOutputSummary(item, payload);

  return buildObservation({
    id: stableId('tq-obs', ['session_item', item.id]),
    traceId,
    parentObservationId,
    sessionId: item.session_id,
    sourceKind: 'session_item',
    sourceId: String(item.id),
    sourceItemId: item.source_item_id,
    observationType: itemObservationType(item),
    name: itemName(item, payload),
    status: item.status,
    statusMessage: payloadStatusMessage(payload, item.status),
    severity: severityFromStatus(item.status),
    toolName: toolNameFromPayload(payload),
    startedAt: item.created_at,
    endedAt: item.created_at,
    durationMs: null,
    inputHash: inputSummary ? payloadHash : null,
    outputHash: outputSummary ? payloadHash : null,
    inputSummary,
    outputSummary,
    payloadPolicy: payloadPolicyForPayload(payload),
    metadata: {
      source_table: 'session_items',
      item_kind: item.kind,
      ordinal: item.ordinal,
      redacted: isRedactedPayload(payload),
    },
  });
}

function projectMessageObservation(traceId: string, message: MessageProjectionSource): ProjectedTraceQualityObservation {
  const contentText = extractContentText(message.content);
  const contentHash = stableHash({
    source: 'messages',
    id: message.id,
    content: message.content,
  });
  const isAssistant = message.role === 'assistant';

  return buildObservation({
    id: stableId('tq-obs', ['message', message.id]),
    traceId,
    sessionId: message.session_id,
    sourceKind: 'message',
    sourceId: String(message.id),
    sourceItemId: String(message.ordinal),
    observationType: messageObservationType(message),
    name: isAssistant ? 'Assistant message' : `${message.role} message`,
    status: 'success',
    severity: 'info',
    startedAt: message.timestamp,
    endedAt: message.timestamp,
    inputHash: isAssistant ? null : contentHash,
    outputHash: isAssistant ? contentHash : null,
    inputSummary: isAssistant ? null : contentText,
    outputSummary: isAssistant ? contentText : null,
    payloadPolicy: 'source_ref',
    metadata: {
      source_table: 'messages',
      role: message.role,
      ordinal: message.ordinal,
      has_thinking: message.has_thinking === 1,
      has_tool_use: message.has_tool_use === 1,
    },
  });
}

function projectToolCallObservation(traceId: string, toolCall: ToolCallProjectionSource): ProjectedTraceQualityObservation {
  const input = parseJsonValue(toolCall.input_json);
  const inputRecord = asRecord(input);
  const inputHash = stableHash({
    source: 'tool_calls',
    id: toolCall.id,
    input: toolCall.input_json,
  });

  return buildObservation({
    id: stableId('tq-obs', ['tool_call', toolCall.id]),
    traceId,
    sessionId: toolCall.session_id,
    sourceKind: 'tool_call',
    sourceId: String(toolCall.id),
    sourceItemId: toolCall.tool_use_id,
    observationType: 'tool',
    name: `Tool: ${toolCall.tool_name}`,
    status: 'success',
    severity: 'info',
    toolName: toolCall.tool_name,
    inputHash: toolCall.input_json ? inputHash : null,
    outputHash: toolCall.result_content ? stableHash(toolCall.result_content) : null,
    inputSummary: inputRecord ? summarizeRecord(inputRecord) : stringifySummaryValue(input),
    outputSummary: summarizeText(toolCall.result_content ?? undefined),
    payloadPolicy: 'source_ref',
    metadata: {
      source_table: 'tool_calls',
      message_id: toolCall.message_id,
      category: toolCall.category,
      subagent_session_id: toolCall.subagent_session_id,
    },
  });
}

function sortedEvents(events: readonly EventProjectionSource[]): EventProjectionSource[] {
  return [...events].sort((a, b) => {
    const aTime = eventTimestamp(a) ?? '';
    const bTime = eventTimestamp(b) ?? '';
    return aTime.localeCompare(bTime) || a.id - b.id;
  });
}

function sortedTurns(turns: readonly SessionTurnProjectionSource[]): SessionTurnProjectionSource[] {
  return [...turns].sort((a, b) => {
    const aTime = a.started_at ?? a.created_at ?? '';
    const bTime = b.started_at ?? b.created_at ?? '';
    return aTime.localeCompare(bTime) || a.id - b.id;
  });
}

function sortedItems(items: readonly SessionItemProjectionSource[]): SessionItemProjectionSource[] {
  return [...items].sort((a, b) => a.ordinal - b.ordinal || a.id - b.id);
}

function sortedMessages(messages: readonly MessageProjectionSource[]): MessageProjectionSource[] {
  return [...messages].sort((a, b) => a.ordinal - b.ordinal || a.id - b.id);
}

function sortedToolCalls(toolCalls: readonly ToolCallProjectionSource[]): ToolCallProjectionSource[] {
  return [...toolCalls].sort((a, b) => a.id - b.id);
}

export function coverageForEvents(events: readonly EventProjectionSource[]): TraceQualityCoverage {
  const hasTokenUsage = events.some(event =>
    event.tokens_in > 0
    || event.tokens_out > 0
    || event.cache_read_tokens > 0
    || event.cache_write_tokens > 0,
  );
  const hasCost = events.some(event => event.cost_usd != null);
  const hasToolDetails = events.some(event => event.event_type === 'tool_use' && Boolean(event.tool_name));
  return {
    has_full_transcript: false,
    has_tool_details: hasToolDetails,
    has_token_usage: hasTokenUsage,
    has_cost: hasCost,
    has_parent_child_structure: false,
    has_raw_input: false,
    has_raw_output: false,
    has_reasoning: false,
    has_prompt_refs: false,
    projection_source: 'events',
    projection_confidence: hasTokenUsage || hasCost ? 'medium' : 'low',
  };
}

function coverageForItems(
  browsingSession: BrowsingSessionProjectionSource | null | undefined,
  source: 'session_turns' | 'browsing_session',
  items: readonly SessionItemProjectionSource[],
): TraceQualityCoverage {
  const parsedPayloads = items.map(item => ({ item, payload: parseJsonRecord(item.payload_json) }));
  const hasToolDetails = items.some(item => item.kind === 'tool_call' || item.kind === 'tool_result' || item.kind === 'command_execution');
  const toolCallIds = new Set(
    items
      .filter(item => item.kind === 'tool_call' && item.source_item_id)
      .map(item => item.source_item_id as string),
  );
  const hasParentChildStructure = items.some(item => item.kind === 'tool_result' && item.source_item_id && toolCallIds.has(item.source_item_id));
  const hasRawInput = parsedPayloads.some(({ item, payload }) =>
    (item.kind === 'user_message' || item.kind === 'tool_call' || item.kind === 'command_execution')
    && !isRedactedPayload(payload),
  );
  const hasRawOutput = parsedPayloads.some(({ item, payload }) =>
    (item.kind === 'assistant_message' || item.kind === 'tool_result')
    && !isRedactedPayload(payload),
  );
  const hasReasoning = parsedPayloads.some(({ item, payload }) =>
    item.kind === 'reasoning' && !isRedactedPayload(payload),
  );
  const confidence = confidenceForBrowsingSession(browsingSession);

  return {
    has_full_transcript: browsingSession?.fidelity === 'full' || browsingSession?.integration_mode === 'claude-jsonl',
    has_tool_details: hasToolDetails,
    has_token_usage: false,
    has_cost: false,
    has_parent_child_structure: hasParentChildStructure,
    has_raw_input: hasRawInput,
    has_raw_output: hasRawOutput,
    has_reasoning: hasReasoning,
    has_prompt_refs: false,
    projection_source: source,
    projection_confidence: confidence === 'unknown' ? 'medium' : confidence,
  };
}

function coverageForMessages(
  browsingSession: BrowsingSessionProjectionSource | null | undefined,
  messages: readonly MessageProjectionSource[],
  toolCalls: readonly ToolCallProjectionSource[],
): TraceQualityCoverage {
  const confidence = confidenceForBrowsingSession(browsingSession);
  return {
    has_full_transcript: messages.length > 0 && (browsingSession?.fidelity === 'full' || browsingSession?.integration_mode === 'claude-jsonl'),
    has_tool_details: toolCalls.length > 0,
    has_token_usage: false,
    has_cost: false,
    has_parent_child_structure: false,
    has_raw_input: messages.some(message => message.role === 'user') || toolCalls.some(toolCall => Boolean(toolCall.input_json)),
    has_raw_output: messages.some(message => message.role === 'assistant') || toolCalls.some(toolCall => Boolean(toolCall.result_content)),
    has_reasoning: messages.some(message => message.has_thinking === 1),
    has_prompt_refs: false,
    projection_source: 'browsing_session',
    projection_confidence: confidence === 'unknown' ? 'medium' : confidence,
  };
}

function projectItemsForTrace(traceId: string, items: readonly SessionItemProjectionSource[]): {
  observations: ProjectedTraceQualityObservation[];
  warnings: string[];
} {
  const observations: ProjectedTraceQualityObservation[] = [];
  const warnings: string[] = [];
  const toolCallObservationBySourceItemId = new Map<string, string>();

  for (const item of sortedItems(items)) {
    const parentObservationId = item.kind === 'tool_result' && item.source_item_id
      ? toolCallObservationBySourceItemId.get(item.source_item_id) ?? null
      : null;
    const observation = projectSessionItemObservation(traceId, item, parentObservationId);
    observations.push(observation);

    if ((item.kind === 'tool_call' || item.kind === 'command_execution') && item.source_item_id) {
      toolCallObservationBySourceItemId.set(item.source_item_id, observation.id);
    }
  }

  return { observations, warnings };
}

function projectEventTraces(input: TraceQualityProjectionInput, events: readonly EventProjectionSource[]): TraceQualityProjectionResult {
  const agentType = normalizeAgentType(input);
  const project = normalizeProject(input);
  const branch = normalizeBranch(input);
  const traces: ProjectedTraceQualityTrace[] = [];
  const observations: ProjectedTraceQualityObservation[] = [];
  const warnings: string[] = [];

  for (const event of sortedEvents(events)) {
    const startedAt = eventTimestamp(event);
    const endedAt = addDuration(startedAt, event.duration_ms);
    const traceId = stableId('tq-trace', ['event', event.id]);
    traces.push(buildTrace({
      id: traceId,
      sessionId: event.session_id,
      browsingSessionId: input.browsingSession?.id ?? null,
      sourceTraceId: event.event_id ?? String(event.id),
      agentType: event.agent_type || agentType,
      name: eventName(event),
      status: event.status,
      project: event.project ?? project,
      branch: event.branch ?? branch,
      startedAt,
      endedAt,
      durationMs: event.duration_ms,
      metadata: {
        source_table: 'events',
        source_id: event.id,
        event_id: event.event_id,
        event_type: event.event_type,
        event_source: event.source,
      },
      tags: ['events'],
      coverage: coverageForEvents([event]),
    }));
    observations.push(projectEventObservation(traceId, event));
  }

  return { traces, observations, warnings };
}

function projectTurnTraces(input: TraceQualityProjectionInput, turns: readonly SessionTurnProjectionSource[], items: readonly SessionItemProjectionSource[]): TraceQualityProjectionResult {
  const agentType = normalizeAgentType(input);
  const project = normalizeProject(input);
  const branch = normalizeBranch(input);
  const traces: ProjectedTraceQualityTrace[] = [];
  const observations: ProjectedTraceQualityObservation[] = [];
  const warnings: string[] = [];

  for (const turn of sortedTurns(turns)) {
    const turnItems = sortedItems(items.filter(item => item.turn_id === turn.id));
    const traceId = stableId('tq-trace', ['session_turn', turn.id]);
    const startedAt = turn.started_at ?? minIso(turnItems.map(item => item.created_at)) ?? turn.created_at;
    const endedAt = turn.ended_at ?? maxIso(turnItems.map(item => item.created_at)) ?? startedAt;
    traces.push(buildTrace({
      id: traceId,
      sessionId: turn.session_id,
      browsingSessionId: input.browsingSession?.id ?? null,
      sourceTraceId: turn.source_turn_id ?? String(turn.id),
      agentType: turn.agent_type || agentType,
      name: turn.title ?? `Turn ${turn.id}`,
      status: turn.status,
      project,
      branch,
      startedAt,
      endedAt,
      metadata: {
        source_table: 'session_turns',
        source_id: turn.id,
        source_turn_id: turn.source_turn_id,
      },
      tags: ['session_turns'],
      coverage: coverageForItems(input.browsingSession, 'session_turns', turnItems),
    }));
    const projectedItems = projectItemsForTrace(traceId, turnItems);
    observations.push(...projectedItems.observations);
    warnings.push(...projectedItems.warnings);
  }

  const turnIds = new Set(turns.map(turn => turn.id));
  const unassignedItems = sortedItems(items.filter(item => item.turn_id == null || !turnIds.has(item.turn_id)));
  if (unassignedItems.length > 0) {
    warnings.push(`${unassignedItems.length} session_items were not attached to a known turn`);
    const fallback = projectBrowsingSessionTrace(input, unassignedItems, [], []);
    traces.push(...fallback.traces);
    observations.push(...fallback.observations);
    warnings.push(...fallback.warnings);
  }

  return { traces, observations, warnings };
}

function projectBrowsingSessionTrace(
  input: TraceQualityProjectionInput,
  items: readonly SessionItemProjectionSource[],
  messages: readonly MessageProjectionSource[],
  toolCalls: readonly ToolCallProjectionSource[],
): TraceQualityProjectionResult {
  const agentType = normalizeAgentType(input);
  const project = normalizeProject(input);
  const branch = normalizeBranch(input);
  const traceId = stableId('tq-trace', ['browsing_session', input.browsingSession?.id ?? input.sessionId]);
  const itemTimes = items.map(item => item.created_at);
  const messageTimes = messages.map(message => message.timestamp);
  const startedAt = input.browsingSession?.started_at ?? minIso([...itemTimes, ...messageTimes]);
  const endedAt = input.browsingSession?.ended_at ?? maxIso([...itemTimes, ...messageTimes]) ?? startedAt;
  const hasItems = items.length > 0;
  const traces = [buildTrace({
    id: traceId,
    sessionId: input.sessionId,
    browsingSessionId: input.browsingSession?.id ?? null,
    sourceTraceId: input.browsingSession?.id ?? input.sessionId,
    agentType,
    name: input.browsingSession?.first_message ?? 'Session trace',
    status: input.browsingSession?.live_status ?? null,
    project,
    branch,
    startedAt,
    endedAt,
    metadata: {
      source_table: 'browsing_sessions',
      source_id: input.browsingSession?.id ?? input.sessionId,
      integration_mode: input.browsingSession?.integration_mode ?? null,
      fidelity: input.browsingSession?.fidelity ?? null,
    },
    tags: hasItems ? ['session_items'] : ['browsing_session'],
    coverage: hasItems
      ? coverageForItems(input.browsingSession, 'browsing_session', items)
      : coverageForMessages(input.browsingSession, messages, toolCalls),
  })];

  const warnings: string[] = [];
  const observations: ProjectedTraceQualityObservation[] = [];

  if (hasItems) {
    const projectedItems = projectItemsForTrace(traceId, items);
    observations.push(...projectedItems.observations);
    warnings.push(...projectedItems.warnings);
  } else {
    observations.push(...sortedMessages(messages).map(message => projectMessageObservation(traceId, message)));
    for (const toolCall of sortedToolCalls(toolCalls)) {
      observations.push(projectToolCallObservation(traceId, toolCall));
    }
  }

  return { traces, observations, warnings };
}

export function projectTraceQuality(input: TraceQualityProjectionInput): TraceQualityProjectionResult {
  const events = sortedEvents(input.events ?? []);
  const turns = sortedTurns(input.turns ?? []);
  const items = sortedItems(input.sessionItems ?? []);
  const messages = sortedMessages(input.messages ?? []);
  const toolCalls = sortedToolCalls(input.toolCalls ?? []);

  if (turns.length > 0) {
    return projectTurnTraces(input, turns, items);
  }

  if (items.length > 0) {
    return projectBrowsingSessionTrace(input, items, [], []);
  }

  if (messages.length > 0 || toolCalls.length > 0 || input.browsingSession) {
    return projectBrowsingSessionTrace(input, [], messages, toolCalls);
  }

  if (events.length > 0) {
    return projectEventTraces(input, events);
  }

  return { traces: [], observations: [], warnings: ['No supported source rows were provided'] };
}
