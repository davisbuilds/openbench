import type { NormalizedIngestEvent, EventType } from '../contracts/event-contract.js';

// ─── OTLP JSON types (subset we care about) ────────────────────────────

interface OtelKeyValue {
  key: string;
  value: OtelAnyValue;
}

interface OtelAnyValue {
  stringValue?: string;
  intValue?: string | number;
  doubleValue?: number;
  boolValue?: boolean;
  kvlistValue?: { values: OtelKeyValue[] };
  arrayValue?: { values: OtelAnyValue[] };
}

interface OtelResource {
  attributes?: OtelKeyValue[];
}

// ─── Logs ───────────────────────────────────────────────────────────────

interface OtelLogRecord {
  timeUnixNano?: string;
  body?: OtelAnyValue;
  attributes?: OtelKeyValue[];
  severityText?: string;
}

interface OtelScopeLogs {
  logRecords?: OtelLogRecord[];
}

interface OtelResourceLogs {
  resource?: OtelResource;
  scopeLogs?: OtelScopeLogs[];
}

export interface OtelLogsPayload {
  resourceLogs?: OtelResourceLogs[];
}

// ─── Metrics ────────────────────────────────────────────────────────────

interface OtelNumberDataPoint {
  asInt?: string | number;
  asDouble?: number;
  attributes?: OtelKeyValue[];
  timeUnixNano?: string;
  startTimeUnixNano?: string;
}

interface OtelSum {
  dataPoints?: OtelNumberDataPoint[];
  isMonotonic?: boolean;
  aggregationTemporality?: number; // 1=delta, 2=cumulative
}

interface OtelGauge {
  dataPoints?: OtelNumberDataPoint[];
}

interface OtelMetric {
  name?: string;
  sum?: OtelSum;
  gauge?: OtelGauge;
}

interface OtelScopeMetrics {
  metrics?: OtelMetric[];
}

interface OtelResourceMetrics {
  resource?: OtelResource;
  scopeMetrics?: OtelScopeMetrics[];
}

export interface OtelMetricsPayload {
  resourceMetrics?: OtelResourceMetrics[];
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

// ─── Attribute helpers ──────────────────────────────────────────────────

function getAttr(attrs: OtelKeyValue[] | undefined, key: string): string | undefined {
  if (!attrs) return undefined;
  const kv = attrs.find(a => a.key === key);
  if (!kv) return undefined;
  if (kv.value.stringValue !== undefined) return kv.value.stringValue;
  if (kv.value.intValue !== undefined) return String(kv.value.intValue);
  if (kv.value.doubleValue !== undefined) return String(kv.value.doubleValue);
  return undefined;
}

function getAttrNumber(attrs: OtelKeyValue[] | undefined, key: string): number | undefined {
  if (!attrs) return undefined;
  const kv = attrs.find(a => a.key === key);
  if (!kv) return undefined;
  if (kv.value.intValue !== undefined) {
    const n = typeof kv.value.intValue === 'number' ? kv.value.intValue : parseInt(kv.value.intValue, 10);
    return Number.isNaN(n) ? undefined : n;
  }
  if (kv.value.doubleValue !== undefined) return kv.value.doubleValue;
  if (kv.value.stringValue !== undefined) {
    const n = parseFloat(kv.value.stringValue);
    return Number.isNaN(n) ? undefined : n;
  }
  return undefined;
}

function getAttrBoolean(attrs: OtelKeyValue[] | undefined, key: string): boolean | undefined {
  if (!attrs) return undefined;
  const kv = attrs.find(a => a.key === key);
  if (!kv) return undefined;
  if (kv.value.boolValue !== undefined) return kv.value.boolValue;
  if (kv.value.stringValue === 'true') return true;
  if (kv.value.stringValue === 'false') return false;
  return undefined;
}

function copyPromptRefAttributes(
  target: Record<string, unknown>,
  attrs: OtelKeyValue[] | undefined,
): void {
  const mappings: Array<[string, string[]]> = [
    ['prompt_name', ['prompt_name', 'prompt.name', 'gen_ai.prompt.name', 'langfuse.prompt.name']],
    ['prompt_version', ['prompt_version', 'prompt.version', 'gen_ai.prompt.version', 'langfuse.prompt.version']],
    ['prompt_label', ['prompt_label', 'prompt.label', 'gen_ai.prompt.label', 'langfuse.prompt.label']],
    ['prompt_hash', ['prompt_hash', 'prompt.hash', 'content_hash', 'gen_ai.prompt.hash', 'langfuse.prompt.hash']],
    ['prompt_source', ['prompt_source', 'prompt.source', 'gen_ai.prompt.source']],
    ['prompt_file_path', ['prompt_file_path', 'prompt.file_path', 'prompt.path']],
  ];

  for (const [metadataKey, attrKeys] of mappings) {
    if (target[metadataKey] != null) continue;
    for (const attrKey of attrKeys) {
      const value = getAttr(attrs, attrKey);
      if (value) {
        target[metadataKey] = value;
        break;
      }
    }
  }
}

function getBodyJson(body: OtelAnyValue | undefined): Record<string, unknown> | undefined {
  if (!body) return undefined;
  if (body.stringValue) {
    try {
      const parsed = JSON.parse(body.stringValue);
      if (typeof parsed === 'object' && parsed !== null) return parsed as Record<string, unknown>;
    } catch {
      // not JSON, ignore
    }
    return undefined;
  }
  if (body.kvlistValue?.values) {
    const obj: Record<string, unknown> = {};
    for (const kv of body.kvlistValue.values) {
      obj[kv.key] = extractAnyValue(kv.value);
    }
    return obj;
  }
  return undefined;
}

function extractAnyValue(v: OtelAnyValue): unknown {
  if (v.stringValue !== undefined) return v.stringValue;
  if (v.intValue !== undefined) return typeof v.intValue === 'number' ? v.intValue : parseInt(v.intValue, 10);
  if (v.doubleValue !== undefined) return v.doubleValue;
  if (v.boolValue !== undefined) return v.boolValue;
  if (v.kvlistValue?.values) {
    const obj: Record<string, unknown> = {};
    for (const kv of v.kvlistValue.values) {
      obj[kv.key] = extractAnyValue(kv.value);
    }
    return obj;
  }
  if (v.arrayValue?.values) {
    return v.arrayValue.values.map(extractAnyValue);
  }
  return undefined;
}

function nanoToIso(nanos: string | undefined): string | undefined {
  if (!nanos) return undefined;
  const ms = Math.floor(Number(BigInt(nanos) / BigInt(1_000_000)));
  if (Number.isNaN(ms) || ms <= 0) return undefined;
  return new Date(ms).toISOString();
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined;
  if (typeof value === 'string') {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function asBoolean(value: unknown): boolean | undefined {
  if (typeof value === 'boolean') return value;
  if (value === 'true') return true;
  if (value === 'false') return false;
  return undefined;
}

function maybeParseJsonString(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function extractTextFromContent(value: unknown): string | undefined {
  if (typeof value === 'string') return value;
  if (!Array.isArray(value)) return undefined;

  const parts = value.flatMap(entry => {
    const record = asRecord(entry);
    const text =
      asString(record?.text)
      ?? asString(record?.content)
      ?? asString(record?.summary);
    return text ? [text] : [];
  });

  return parts.length > 0 ? parts.join('\n') : undefined;
}

function splitCommaSeparatedList(value: string | undefined): string[] | undefined {
  if (!value) return undefined;
  const items = value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean);
  return items.length > 0 ? items : undefined;
}

// ─── Patch parsing helper ────────────────────────────────────────────────

export interface PatchMeta {
  file_path: string;
  lines_added: number;
  lines_removed: number;
}

/** Extract file path and line counts from a Codex apply_patch input string. */
export function parsePatchMeta(input: string): PatchMeta | null {
  // Match "*** (Update|Add|Delete) File: <path>"
  const fileMatch = input.match(/\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)/);
  if (!fileMatch) return null;

  const filePath = fileMatch[1].trim();
  let added = 0;
  let removed = 0;

  for (const line of input.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) added++;
    else if (line.startsWith('-') && !line.startsWith('---')) removed++;
  }

  return { file_path: filePath, lines_added: added, lines_removed: removed };
}

// ─── Event name → event_type mapping ────────────────────────────────────

const CLAUDE_EVENT_MAP: Record<string, EventType> = {
  'claude_code.tool_result': 'tool_use',
  'claude_code.tool_use': 'tool_use',
  'claude_code.api_request': 'llm_request',
  'claude_code.api_response': 'llm_response',
  'claude_code.session_start': 'session_start',
  'claude_code.session_end': 'session_end',
  'claude_code.file_change': 'file_change',
  'claude_code.git_commit': 'git_commit',
  'claude_code.plan_step': 'plan_step',
  'claude_code.error': 'error',
  'claude_code.user_prompt': 'user_prompt',
  'claude_code.user_prompt_submit': 'user_prompt',
};

const CODEX_EVENT_MAP: Record<string, EventType> = {
  'codex.tool_result': 'tool_use',
  'codex.tool_use': 'tool_use',
  'codex.tool_decision': 'tool_use',
  'codex.api_request': 'llm_request',
  'codex.api_response': 'llm_response',
  'codex.conversation_starts': 'session_start',
  'codex.session_start': 'session_start',
  'codex.session_end': 'session_end',
  'codex.websocket_request': 'llm_request',
  'codex.file_change': 'file_change',
  'codex.error': 'error',
  'codex.user_prompt': 'user_prompt',
  'codex.user_message': 'user_prompt',
};

// High-frequency events to skip — these create noise without useful signal
const SKIP_EVENTS = new Set([
  'claude_code.response',
]);

const SKIPPED_CODEX_WEBSOCKET_RESPONSE_KINDS = new Set([
  'response.custom_tool_call_input.delta',
  'response.function_call_arguments.delta',
  'response.output_text.delta',
  'response.created',
  'response.in_progress',
  'response.output_item.added',
  'response.output_item.done',
  'response.content_part.added',
  'response.content_part.done',
  'response.output_text.done',
  'responsesapi.websocket_timing',
]);

const CODEX_RESPONSE_ITEM_TYPES = new Set([
  'assistant_message',
  'agent_message',
  'message_from_assistant',
  'message_from_user',
  'reasoning',
  'reasoning_summary_delta',
  'reasoning_content_delta',
  'reasoning_summary_part_added',
  'local_shell_call',
  'function_call',
  'function_call_output',
  'tool_search_call',
  'tool_search_output',
  'custom_tool_call',
  'custom_tool_call_output',
  'web_search_call',
  'image_generation_call',
  'ghost_snapshot',
  'compaction',
  'other',
]);

function getCodexPayload(logBody: Record<string, unknown> | undefined): Record<string, unknown> | undefined {
  return asRecord(logBody?.payload);
}

function getCodexPayloadType(
  logRecord: OtelLogRecord,
  bodyJson: Record<string, unknown> | undefined,
): string | undefined {
  const payload = getCodexPayload(bodyJson);
  return (
    asString(payload?.type)
    ?? asString(bodyJson?.type)
    ?? getAttr(logRecord.attributes, 'response_item.type')
    ?? getAttr(logRecord.attributes, 'item.type')
    ?? getAttr(logRecord.attributes, 'type')
  );
}

function getCodexEventKind(
  logRecord: OtelLogRecord,
  bodyJson: Record<string, unknown> | undefined,
): string | undefined {
  const payload = getCodexPayload(bodyJson);
  return (
    getAttr(logRecord.attributes, 'event.kind')
    ?? getAttr(logRecord.attributes, 'kind')
    ?? asString(bodyJson?.event_kind)
    ?? asString(bodyJson?.kind)
    ?? asString(payload?.event_kind)
    ?? asString(payload?.kind)
  );
}

function getCodexErrorMessage(
  logRecord: OtelLogRecord,
  bodyJson: Record<string, unknown> | undefined,
): string | undefined {
  const payload = getCodexPayload(bodyJson);
  const nestedError = asRecord(bodyJson?.error);
  const nestedPayloadError = asRecord(payload?.error);
  return (
    getAttr(logRecord.attributes, 'error.message')
    ?? asString(bodyJson?.error)
    ?? asString(payload?.error)
    ?? asString(nestedError?.message)
    ?? asString(nestedPayloadError?.message)
  );
}

function hasCodexTransportFailure(
  logRecord: OtelLogRecord,
  bodyJson: Record<string, unknown> | undefined,
): boolean {
  return (
    getAttrBoolean(logRecord.attributes, 'success') === false
    || asBoolean(bodyJson?.success) === false
    || asBoolean(getCodexPayload(bodyJson)?.success) === false
    || Boolean(getCodexErrorMessage(logRecord, bodyJson))
  );
}

// ─── Log parser ─────────────────────────────────────────────────────────

function resolveServiceName(resourceAttrs: OtelKeyValue[] | undefined): string {
  const svc = getAttr(resourceAttrs, 'service.name') ?? '';
  const sdk = getAttr(resourceAttrs, 'telemetry.sdk.name') ?? '';
  const combined = `${svc} ${sdk}`.toLowerCase();
  if (combined.includes('codex')) return 'codex';
  if (combined.includes('claude')) return 'claude_code';
  return svc || 'unknown';
}

function resolveEventType(
  logRecord: OtelLogRecord,
  agentType: string,
  eventName: string | undefined,
  bodyJson: Record<string, unknown> | undefined,
): EventType | null {

  if (eventName) {
    const map = agentType === 'codex' ? CODEX_EVENT_MAP : CLAUDE_EVENT_MAP;
    if (map[eventName]) return map[eventName];

    if (agentType === 'codex') {
      const codexMessageType = getCodexPayloadType(logRecord, bodyJson);
      const codexEventKind = getCodexEventKind(logRecord, bodyJson);
      if ((eventName === 'codex.response' || eventName === 'codex.event_msg')
        && (codexMessageType === 'user_message' || codexMessageType === 'user_prompt')) {
        return 'user_prompt';
      }
      if ((eventName === 'codex.response' || eventName === 'codex.event_msg')
        && codexMessageType && CODEX_RESPONSE_ITEM_TYPES.has(codexMessageType)) {
        return 'response';
      }
      if (eventName === 'codex.sse_event') {
        if (codexEventKind === 'response.completed') return 'llm_response';
        if (codexEventKind === 'response.failed' || hasCodexTransportFailure(logRecord, bodyJson)) {
          return 'error';
        }
        if (codexEventKind?.startsWith('response.')) return 'response';
        return null;
      }
      if (eventName === 'codex.websocket_event' || eventName === 'codex.websocket.event') {
        if (codexEventKind === 'response.failed' || hasCodexTransportFailure(logRecord, bodyJson)) {
          return 'error';
        }
        if (codexEventKind && SKIPPED_CODEX_WEBSOCKET_RESPONSE_KINDS.has(codexEventKind)) {
          return null;
        }
        if (codexEventKind?.startsWith('response.')) {
          return 'response';
        }
        return null;
      }
    }

    // Try generic suffix matching (e.g. "tool_use" from "some_prefix.tool_use")
    const suffix = eventName.split('.').pop() ?? '';
    const EVENT_TYPE_SUFFIXES: Record<string, EventType> = {
      tool_result: 'tool_use',
      tool_use: 'tool_use',
      api_request: 'llm_request',
      api_response: 'llm_response',
      session_start: 'session_start',
      session_end: 'session_end',
      file_change: 'file_change',
      git_commit: 'git_commit',
      plan_step: 'plan_step',
      error: 'error',
      user_prompt: 'user_prompt',
      user_prompt_submit: 'user_prompt',
      user_message: 'user_prompt',
    };
    if (EVENT_TYPE_SUFFIXES[suffix]) return EVENT_TYPE_SUFFIXES[suffix];
  }

  if (agentType === 'codex') {
    const codexPayloadType = getCodexPayloadType(logRecord, bodyJson);
    if (codexPayloadType === 'user_message' || codexPayloadType === 'user_prompt') {
      return 'user_prompt';
    }
    if (codexPayloadType && CODEX_RESPONSE_ITEM_TYPES.has(codexPayloadType)) {
      return 'response';
    }
  }

  // Fallback: check severity
  if (logRecord.severityText === 'ERROR') return 'error';

  // Unrecognized event — skip to avoid noise
  return null;
}

function parseLogRecord(
  logRecord: OtelLogRecord,
  resourceAttrs: OtelKeyValue[] | undefined,
): NormalizedIngestEvent | null {
  const bodyJson = getBodyJson(logRecord.body);
  const payload = getCodexPayload(bodyJson);

  // Skip high-frequency noisy events
  const eventName = getAttr(logRecord.attributes, 'event.name');
  if (eventName && SKIP_EVENTS.has(eventName)) return null;

  const agentType = resolveServiceName(resourceAttrs);

  // Session ID: prefer log attribute, then resource attribute, then body
  const sessionId =
    getAttr(logRecord.attributes, 'gen_ai.session.id')
    ?? getAttr(logRecord.attributes, 'conversation.id')
    ?? getAttr(resourceAttrs, 'session.id')
    ?? getAttr(resourceAttrs, 'gen_ai.session.id')
    ?? getAttr(resourceAttrs, 'conversation.id')
    ?? (bodyJson?.session_id as string | undefined);

  if (!sessionId) return null; // Cannot process without session_id

  const resolvedEventName = eventName ?? getAttr(logRecord.attributes, 'name');
  const eventType = resolveEventType(logRecord, agentType, resolvedEventName, bodyJson);
  if (!eventType) return null; // Skip unrecognized events

  // Extract fields from attributes + body
  const toolName =
    getAttr(logRecord.attributes, 'gen_ai.tool.name')
    ?? getAttr(logRecord.attributes, 'tool_name')
    ?? getAttr(logRecord.attributes, 'tool.name')
    ?? asString(bodyJson?.tool_name)
    ?? asString(bodyJson?.name)
    ?? asString(payload?.tool_name)
    ?? asString(payload?.name);

  const model =
    getAttr(logRecord.attributes, 'gen_ai.request.model')
    ?? getAttr(logRecord.attributes, 'model')
    ?? (bodyJson?.model as string | undefined);

  const tokensIn =
    getAttrNumber(logRecord.attributes, 'gen_ai.usage.input_tokens')
    ?? getAttrNumber(logRecord.attributes, 'input_token_count')
    ?? asNumber(bodyJson?.input_tokens)
    ?? asNumber(bodyJson?.input_token_count)
    ?? asNumber(payload?.input_tokens)
    ?? asNumber(payload?.input_token_count)
    ?? 0;

  const tokensOut =
    getAttrNumber(logRecord.attributes, 'gen_ai.usage.output_tokens')
    ?? getAttrNumber(logRecord.attributes, 'output_token_count')
    ?? asNumber(bodyJson?.output_tokens)
    ?? asNumber(bodyJson?.output_token_count)
    ?? asNumber(payload?.output_tokens)
    ?? asNumber(payload?.output_token_count)
    ?? 0;

  // Anthropic reports `input_tokens` already net of cache (cache_read is a
  // separate, additive bucket). OpenAI/Codex report `input_token(s)` as
  // cache-inclusive, with the cached count a subset of it. Distinguish by the
  // field name that supplies the cache figure rather than by model, because
  // Codex SSE records carry no model attribute.
  const anthropicCacheRead =
    getAttrNumber(logRecord.attributes, 'gen_ai.usage.cache_read_input_tokens')
    ?? asNumber(bodyJson?.cache_read_tokens)
    ?? asNumber(payload?.cache_read_tokens);
  const openaiCacheRead =
    getAttrNumber(logRecord.attributes, 'cached_token_count')
    ?? asNumber(bodyJson?.cached_token_count)
    ?? asNumber(bodyJson?.cached_input_tokens)
    ?? asNumber(payload?.cached_token_count)
    ?? asNumber(payload?.cached_input_tokens);
  const cacheReadTokens = anthropicCacheRead ?? openaiCacheRead ?? 0;

  const anthropicCacheWrite =
    getAttrNumber(logRecord.attributes, 'gen_ai.usage.cache_creation_input_tokens');
  const normalizedCacheWrite =
    getAttrNumber(logRecord.attributes, 'cache_write_tokens')
    ?? asNumber(bodyJson?.cache_write_tokens)
    ?? asNumber(payload?.cache_write_tokens);
  const cacheWriteTokens = anthropicCacheWrite ?? normalizedCacheWrite ?? 0;

  // `cache_write_tokens` is a normalized field used by more than one provider.
  // It is part of inclusive input only when the record itself identifies an
  // OpenAI/Codex source; for Claude/Anthropic it remains an additive bucket.
  const openaiCacheWrite = (
    agentType === 'codex'
    || agentType.toLowerCase().includes('openai')
    || (model ? /^gpt-|^o\d/.test(model.replace(/^openai\//, '').toLowerCase()) : false)
  )
    ? normalizedCacheWrite
    : undefined;

  // Only subtract when the cache figures came from cache-inclusive OpenAI/Codex
  // fields and no Anthropic-style net field was present. Both cached reads and
  // GPT-5.6 cache writes are subsets of the reported total input tokens.
  const cacheInclusiveInput = anthropicCacheRead == null
    && anthropicCacheWrite == null
    && ((openaiCacheRead ?? 0) > 0 || (openaiCacheWrite ?? 0) > 0);
  const tokensInNet = cacheInclusiveInput
    ? Math.max(0, (tokensIn as number) - cacheReadTokens - cacheWriteTokens)
    : (tokensIn as number);

  const costUsd =
    getAttrNumber(logRecord.attributes, 'gen_ai.usage.cost')
    ?? asNumber(bodyJson?.cost_usd)
    ?? asNumber(payload?.cost_usd);

  const durationMs =
    getAttrNumber(logRecord.attributes, 'gen_ai.latency')
    ?? getAttrNumber(logRecord.attributes, 'duration_ms')
    ?? (typeof bodyJson?.duration_ms === 'number' ? bodyJson.duration_ms : undefined);

  const project =
    getAttr(logRecord.attributes, 'project')
    ?? getAttr(resourceAttrs, 'project')
    ?? (bodyJson?.project as string | undefined);

  const branch =
    getAttr(logRecord.attributes, 'branch')
    ?? getAttr(resourceAttrs, 'branch')
    ?? (bodyJson?.branch as string | undefined);

  const clientTimestamp = nanoToIso(logRecord.timeUnixNano);

  // Build metadata from body JSON (minus fields we've already extracted)
  let metadata: unknown = {};
  if (bodyJson) {
    const extracted = new Set([
      'session_id', 'tool_name', 'model', 'input_tokens', 'output_tokens',
      'cache_read_tokens', 'cache_write_tokens', 'cost_usd', 'duration_ms',
      'input_token_count', 'output_token_count', 'cached_token_count', 'cached_input_tokens',
      'project', 'branch',
    ]);
    const remaining: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(bodyJson)) {
      if (!extracted.has(k)) remaining[k] = v;
    }
    if (Object.keys(remaining).length > 0) metadata = remaining;
  } else if (logRecord.body?.stringValue) {
    // Plain-string body (non-JSON) — store as message
    metadata = { message: logRecord.body.stringValue };
  }

  if (typeof metadata !== 'object' || metadata === null || Array.isArray(metadata)) {
    metadata = {};
  }
  const meta = metadata as Record<string, unknown>;

  if (resolvedEventName) {
    meta.otel_event_name ??= resolvedEventName;
  }
  copyPromptRefAttributes(meta, logRecord.attributes);
  copyPromptRefAttributes(meta, resourceAttrs);

  if (agentType === 'codex') {
    const codexEventKind = getCodexEventKind(logRecord, bodyJson);
    const codexPayloadType = getCodexPayloadType(logRecord, bodyJson);
    const errorMessage = getCodexErrorMessage(logRecord, bodyJson);
    const success =
      getAttrBoolean(logRecord.attributes, 'success')
      ?? asBoolean(bodyJson?.success)
      ?? asBoolean(payload?.success);

    if (codexEventKind) meta.event_kind ??= codexEventKind;
    if (errorMessage) meta.error ??= errorMessage;
    if (success !== undefined) meta.success ??= success;

    if ((resolvedEventName === 'codex.response' || resolvedEventName === 'codex.event_msg') && codexPayloadType) {
      meta.response_item_type ??= codexPayloadType;
    }

    if (resolvedEventName === 'codex.conversation_starts') {
      const mcpServers = splitCommaSeparatedList(
        getAttr(logRecord.attributes, 'mcp_servers')
        ?? asString(bodyJson?.mcp_servers)
        ?? asString(payload?.mcp_servers)
      );

      meta.provider_name ??= getAttr(logRecord.attributes, 'provider_name') ?? asString(bodyJson?.provider_name);
      meta.reasoning_effort ??= getAttr(logRecord.attributes, 'reasoning_effort') ?? asString(bodyJson?.reasoning_effort);
      meta.reasoning_summary ??= getAttr(logRecord.attributes, 'reasoning_summary') ?? asString(bodyJson?.reasoning_summary);
      meta.context_window ??= getAttrNumber(logRecord.attributes, 'context_window') ?? asNumber(bodyJson?.context_window);
      meta.auto_compact_token_limit ??= (
        getAttrNumber(logRecord.attributes, 'auto_compact_token_limit')
        ?? asNumber(bodyJson?.auto_compact_token_limit)
      );
      meta.approval_policy ??= getAttr(logRecord.attributes, 'approval_policy') ?? asString(bodyJson?.approval_policy);
      meta.sandbox_policy ??= getAttr(logRecord.attributes, 'sandbox_policy') ?? asString(bodyJson?.sandbox_policy);
      meta.active_profile ??= getAttr(logRecord.attributes, 'active_profile') ?? asString(bodyJson?.active_profile);
      if (mcpServers) meta.mcp_servers ??= mcpServers;
    }

    if (resolvedEventName === 'codex.api_request' || resolvedEventName === 'codex.websocket_request' || resolvedEventName === 'codex.websocket_connect') {
      meta.http_status_code ??= (
        getAttrNumber(logRecord.attributes, 'http.response.status_code')
        ?? asNumber(bodyJson?.http_status_code)
      );
      meta.endpoint ??= getAttr(logRecord.attributes, 'endpoint') ?? asString(bodyJson?.endpoint);
      meta.request_id ??= getAttr(logRecord.attributes, 'auth.request_id') ?? asString(bodyJson?.request_id);
      meta.connection_reused ??= (
        getAttrBoolean(logRecord.attributes, 'auth.connection_reused')
        ?? asBoolean(bodyJson?.connection_reused)
      );
    }

    if (resolvedEventName === 'codex.tool_result' || resolvedEventName === 'codex.tool_decision') {
      const argumentsRaw =
        getAttr(logRecord.attributes, 'arguments')
        ?? asString(bodyJson?.arguments)
        ?? asString(payload?.arguments)
        ?? asString(bodyJson?.input)
        ?? asString(payload?.input);
      const outputRaw =
        getAttr(logRecord.attributes, 'output')
        ?? asString(bodyJson?.output)
        ?? asString(payload?.output);

      meta.call_id ??= getAttr(logRecord.attributes, 'call_id') ?? asString(bodyJson?.call_id) ?? asString(payload?.call_id);
      meta.decision ??= getAttr(logRecord.attributes, 'decision') ?? asString(bodyJson?.decision);
      meta.decision_source ??= getAttr(logRecord.attributes, 'source') ?? asString(bodyJson?.source);
      meta.mcp_server ??= getAttr(logRecord.attributes, 'mcp_server') ?? asString(bodyJson?.mcp_server);
      meta.mcp_server_origin ??= getAttr(logRecord.attributes, 'mcp_server_origin') ?? asString(bodyJson?.mcp_server_origin);

      if (argumentsRaw !== undefined) {
        meta.arguments ??= maybeParseJsonString(argumentsRaw);
      }
      if (outputRaw !== undefined) {
        meta.output ??= outputRaw;
        meta.content_preview ??= outputRaw.slice(0, 500);
      }
    }

    if (resolvedEventName === 'codex.sse_event') {
      meta.reasoning_token_count ??= (
        getAttrNumber(logRecord.attributes, 'reasoning_token_count')
        ?? asNumber(bodyJson?.reasoning_token_count)
      );
      meta.tool_token_count ??= (
        getAttrNumber(logRecord.attributes, 'tool_token_count')
        ?? asNumber(bodyJson?.tool_token_count)
      );
    }

    if (resolvedEventName === 'codex.response' || resolvedEventName === 'codex.event_msg') {
      const extractedText =
        asString(payload?.message)
        ?? asString(bodyJson?.message)
        ?? asString(payload?.text)
        ?? asString(bodyJson?.text)
        ?? extractTextFromContent(payload?.content)
        ?? extractTextFromContent(bodyJson?.content)
        ?? asString(payload?.content_preview)
        ?? asString(bodyJson?.content_preview);

      if (eventType === 'user_prompt' && extractedText) {
        meta.message ??= extractedText;
      }
      if (eventType === 'response' && extractedText) {
        meta.text ??= extractedText;
        meta.content_preview ??= extractedText.slice(0, 500);
      }
    }
  }

  // For user_prompt events, ensure we capture the prompt text from all possible sources
  if (eventType === 'user_prompt') {
    const meta = (typeof metadata === 'object' && metadata !== null) ? metadata as Record<string, unknown> : {};
    if (!meta.message) {
      const payload = asRecord(bodyJson?.payload);
      const bodyPromptText =
        (typeof bodyJson?.message === 'string' ? bodyJson.message : undefined)
        ?? (typeof payload?.message === 'string' ? payload.message : undefined);
      // Try attributes: gen_ai.prompt, message, prompt, codex.prompt
      const promptText =
        bodyPromptText
        ?? getAttr(logRecord.attributes, 'gen_ai.prompt')
        ?? getAttr(logRecord.attributes, 'message')
        ?? getAttr(logRecord.attributes, 'prompt')
        ?? getAttr(logRecord.attributes, 'codex.prompt')
        ?? getAttr(logRecord.attributes, 'gen_ai.content.prompt');
      if (promptText) {
        metadata = { ...meta, message: promptText };
      } else if (logRecord.body?.stringValue && !bodyJson) {
        // Non-JSON string body — already handled above, but guard for edge cases
        metadata = { ...meta, message: logRecord.body.stringValue };
      }
    }
  }

  // For apply_patch tool events, extract file path and line counts from patch input
  if (toolName === 'apply_patch') {
    const meta = (typeof metadata === 'object' && metadata !== null) ? metadata as Record<string, unknown> : {};
    const patchInput = typeof meta.input === 'string'
      ? meta.input
      : typeof meta.arguments === 'string'
        ? meta.arguments
        : typeof bodyJson?.input === 'string'
          ? bodyJson.input
          : undefined;
    if (patchInput) {
      const patchMeta = parsePatchMeta(patchInput);
      if (patchMeta) {
        metadata = {
          ...meta,
          file_path: patchMeta.file_path,
          lines_added: patchMeta.lines_added,
          lines_removed: patchMeta.lines_removed,
        };
      }
    }
  }

  const status = (
    eventType === 'error'
    || getAttrBoolean(logRecord.attributes, 'success') === false
    || asBoolean(bodyJson?.success) === false
    || asBoolean(payload?.success) === false
  ) ? 'error' : 'success';

  return {
    session_id: sessionId,
    agent_type: agentType,
    event_type: eventType,
    tool_name: toolName,
    status,
    tokens_in: tokensInNet,
    tokens_out: tokensOut as number,
    cache_read_tokens: cacheReadTokens as number,
    cache_write_tokens: cacheWriteTokens as number,
    model,
    cost_usd: costUsd,
    duration_ms: durationMs,
    project,
    branch,
    client_timestamp: clientTimestamp,
    metadata,
    source: 'otel',
  };
}

export function parseOtelLogs(payload: OtelLogsPayload): NormalizedIngestEvent[] {
  const events: NormalizedIngestEvent[] = [];

  if (!payload.resourceLogs) return events;

  for (const rl of payload.resourceLogs) {
    const resourceAttrs = rl.resource?.attributes;
    if (!rl.scopeLogs) continue;

    for (const sl of rl.scopeLogs) {
      if (!sl.logRecords) continue;

      for (const lr of sl.logRecords) {
        const event = parseLogRecord(lr, resourceAttrs);
        if (event) events.push(event);
      }
    }
  }

  return events;
}

// ─── Metric parser with cumulative-to-delta ─────────────────────────────

// In-memory store for cumulative-to-delta conversion.
// Keyed by `service|metricName|model|type` → last seen value.
const cumulativeState = new Map<string, number>();

export interface ParsedMetricDelta {
  session_id: string;
  agent_type: string;
  model?: string;
  tokens_in_delta: number;
  tokens_out_delta: number;
  cache_read_delta: number;
  cache_write_delta: number;
  cost_usd_delta: number;
}

function getDataPointValue(dp: OtelNumberDataPoint): number {
  if (dp.asDouble !== undefined) return dp.asDouble;
  if (dp.asInt !== undefined) {
    return typeof dp.asInt === 'number' ? dp.asInt : parseInt(dp.asInt, 10);
  }
  return 0;
}

function computeDelta(key: string, currentValue: number): number {
  const lastValue = cumulativeState.get(key);
  cumulativeState.set(key, currentValue);

  if (lastValue === undefined) {
    // First time seeing this metric — treat current value as the delta
    return currentValue;
  }

  const delta = currentValue - lastValue;
  // Skip if delta <= 0 (counter reset or no change)
  return delta > 0 ? delta : 0;
}

const TOKEN_METRICS = new Set([
  'claude_code.token.usage',
  'codex_cli_rs.token.usage',
  'gen_ai.client.token.usage',
]);

const COST_METRICS = new Set([
  'claude_code.cost.usage',
  'codex_cli_rs.cost.usage',
  'gen_ai.client.cost.usage',
]);

export function parseOtelMetrics(payload: OtelMetricsPayload): ParsedMetricDelta[] {
  const results: ParsedMetricDelta[] = [];

  if (!payload.resourceMetrics) return results;

  for (const rm of payload.resourceMetrics) {
    const resourceAttrs = rm.resource?.attributes;
    const agentType = resolveServiceName(resourceAttrs);
    const sessionId =
      getAttr(resourceAttrs, 'gen_ai.session.id')
      ?? getAttr(resourceAttrs, 'session.id')
      ?? getAttr(resourceAttrs, 'conversation.id')
      ?? 'unknown';

    if (!rm.scopeMetrics) continue;

    for (const sm of rm.scopeMetrics) {
      if (!sm.metrics) continue;

      for (const metric of sm.metrics) {
        const metricName = metric.name ?? '';
        const dataPoints = metric.sum?.dataPoints ?? metric.gauge?.dataPoints ?? [];
        const isCumulative = metric.sum?.aggregationTemporality === 2;

        for (const dp of dataPoints) {
          const rawValue = getDataPointValue(dp);
          const model = getAttr(dp.attributes, 'model')
            ?? getAttr(dp.attributes, 'gen_ai.request.model')
            ?? getAttr(resourceAttrs, 'model');
          const tokenType = getAttr(dp.attributes, 'type')
            ?? getAttr(dp.attributes, 'token.type');

          const cacheKey = `${sessionId}|${agentType}|${metricName}|${model ?? ''}|${tokenType ?? ''}`;
          const delta = isCumulative ? computeDelta(cacheKey, rawValue) : rawValue;

          if (delta <= 0) continue;

          if (TOKEN_METRICS.has(metricName)) {
            const entry: ParsedMetricDelta = {
              session_id: sessionId,
              agent_type: agentType,
              model: model ?? undefined,
              tokens_in_delta: 0,
              tokens_out_delta: 0,
              cache_read_delta: 0,
              cache_write_delta: 0,
              cost_usd_delta: 0,
            };

            switch (tokenType) {
              case 'input':
                entry.tokens_in_delta = delta;
                break;
              case 'output':
                entry.tokens_out_delta = delta;
                break;
              case 'cacheRead':
              case 'cache_read':
                entry.cache_read_delta = delta;
                break;
              case 'cacheCreation':
              case 'cache_creation':
              case 'cache_write':
                entry.cache_write_delta = delta;
                break;
              default:
                // Unknown token type — default to input
                entry.tokens_in_delta = delta;
            }

            results.push(entry);
          } else if (COST_METRICS.has(metricName)) {
            results.push({
              session_id: sessionId,
              agent_type: agentType,
              model: model ?? undefined,
              tokens_in_delta: 0,
              tokens_out_delta: 0,
              cache_read_delta: 0,
              cache_write_delta: 0,
              cost_usd_delta: delta,
            });
          }
        }
      }
    }
  }

  return results;
}

// Exposed for testing — reset cumulative state
export function resetCumulativeState(): void {
  cumulativeState.clear();
}
