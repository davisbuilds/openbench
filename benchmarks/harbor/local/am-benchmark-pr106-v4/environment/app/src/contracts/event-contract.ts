const EVENT_TYPES = [
  'tool_use',
  'session_start',
  'session_end',
  'error',
  'llm_request',
  'llm_response',
  'response',
  'file_change',
  'git_commit',
  'plan_step',
  'user_prompt',
  'instruction_load',
] as const;

const EVENT_STATUSES = [
  'success',
  'error',
  'timeout',
] as const;

const _EVENT_SOURCES = [
  'api',
  'hook',
  'otel',
  'import',
  // Batch-imported external benchmark runs (openbench results.jsonl). Segregated
  // from the default cost/usage/analytics aggregates so a bake-off does not
  // pollute real-activity numbers; opt in via UsageParams.include_benchmark.
  'benchmark',
] as const;

const EVENT_TYPE_SET = new Set<string>(EVENT_TYPES);
const EVENT_STATUS_SET = new Set<string>(EVENT_STATUSES);

export type EventType = (typeof EVENT_TYPES)[number];
export type EventStatus = (typeof EVENT_STATUSES)[number];
export type EventSource = (typeof _EVENT_SOURCES)[number];

export interface NormalizedIngestEvent {
  event_id?: string;
  session_id: string;
  agent_type: string;
  event_type: EventType;
  tool_name?: string;
  status: EventStatus;
  tokens_in: number;
  tokens_out: number;
  branch?: string;
  project?: string;
  duration_ms?: number;
  metadata: unknown;
  client_timestamp?: string;
  model?: string;
  cost_usd?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  source?: EventSource;
  // Invocation mode of the owning session (interactive vs headless). Session-level
  // constant carried on events so the session upsert can persist it; see
  // src/util/invocation-mode.ts.
  mode?: 'interactive' | 'headless';
}

export interface ContractValidationError {
  field: string;
  message: string;
}

export type NormalizeEventResult =
  | { ok: true; event: NormalizedIngestEvent }
  | { ok: false; errors: ContractValidationError[] };

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function getRequiredString(
  input: Record<string, unknown>,
  field: string,
  errors: ContractValidationError[]
): string {
  const raw = input[field];
  if (typeof raw !== 'string') {
    errors.push({ field, message: 'must be a string' });
    return '';
  }

  const value = raw.trim();
  if (!value) {
    errors.push({ field, message: 'must be a non-empty string' });
  }
  return value;
}

function getOptionalString(
  input: Record<string, unknown>,
  field: string,
  errors: ContractValidationError[]
): string | undefined {
  const raw = input[field];
  if (raw === undefined || raw === null) return undefined;
  if (typeof raw !== 'string') {
    errors.push({ field, message: 'must be a string when provided' });
    return undefined;
  }

  const value = raw.trim();
  return value || undefined;
}

function getOptionalNonNegativeInt(
  input: Record<string, unknown>,
  field: string,
  errors: ContractValidationError[]
): number | undefined {
  const raw = input[field];
  if (raw === undefined || raw === null) return undefined;
  if (typeof raw !== 'number' || !Number.isInteger(raw) || raw < 0) {
    errors.push({ field, message: 'must be a non-negative integer when provided' });
    return undefined;
  }
  return raw;
}

function getOptionalNonNegativeNumber(
  input: Record<string, unknown>,
  field: string,
  errors: ContractValidationError[]
): number | undefined {
  const raw = input[field];
  if (raw === undefined || raw === null) return undefined;
  if (typeof raw !== 'number' || raw < 0) {
    errors.push({ field, message: 'must be a non-negative number when provided' });
    return undefined;
  }
  return raw;
}

function normalizeEventType(
  value: string,
  errors: ContractValidationError[]
): EventType {
  if (!EVENT_TYPE_SET.has(value)) {
    errors.push({
      field: 'event_type',
      message: `must be one of: ${EVENT_TYPES.join(', ')}`,
    });
  }
  return value as EventType;
}

function normalizeStatus(
  input: Record<string, unknown>,
  eventType: EventType,
  errors: ContractValidationError[]
): EventStatus {
  const raw = input.status;
  if (raw === undefined || raw === null) {
    return eventType === 'error' ? 'error' : 'success';
  }
  if (typeof raw !== 'string') {
    errors.push({ field: 'status', message: 'must be a string when provided' });
    return eventType === 'error' ? 'error' : 'success';
  }
  if (!EVENT_STATUS_SET.has(raw)) {
    errors.push({
      field: 'status',
      message: `must be one of: ${EVENT_STATUSES.join(', ')}`,
    });
  }
  return raw as EventStatus;
}

function normalizeClientTimestamp(
  input: Record<string, unknown>,
  errors: ContractValidationError[]
): string | undefined {
  const raw = input.client_timestamp;
  if (raw === undefined || raw === null) return undefined;
  if (typeof raw !== 'string') {
    errors.push({ field: 'client_timestamp', message: 'must be an ISO timestamp string when provided' });
    return undefined;
  }

  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) {
    errors.push({ field: 'client_timestamp', message: 'must be a valid timestamp' });
    return undefined;
  }
  return parsed.toISOString();
}

export function normalizeIngestEvent(input: unknown): NormalizeEventResult {
  if (!isRecord(input)) {
    return {
      ok: false,
      errors: [{ field: 'body', message: 'must be a JSON object' }],
    };
  }

  const errors: ContractValidationError[] = [];

  const sessionId = getRequiredString(input, 'session_id', errors);
  const agentType = getRequiredString(input, 'agent_type', errors);
  const eventTypeRaw = getRequiredString(input, 'event_type', errors);
  const eventType = normalizeEventType(eventTypeRaw, errors);
  const status = normalizeStatus(input, eventType, errors);

  const eventId = getOptionalString(input, 'event_id', errors);
  const toolName = getOptionalString(input, 'tool_name', errors);
  const branch = getOptionalString(input, 'branch', errors);
  const project = getOptionalString(input, 'project', errors);
  const model = getOptionalString(input, 'model', errors);
  const durationMs = getOptionalNonNegativeInt(input, 'duration_ms', errors);
  const tokensIn = getOptionalNonNegativeInt(input, 'tokens_in', errors) ?? 0;
  const tokensOut = getOptionalNonNegativeInt(input, 'tokens_out', errors) ?? 0;
  const cacheReadTokens = getOptionalNonNegativeInt(input, 'cache_read_tokens', errors) ?? 0;
  const cacheWriteTokens = getOptionalNonNegativeInt(input, 'cache_write_tokens', errors) ?? 0;
  const costUsd = getOptionalNonNegativeNumber(input, 'cost_usd', errors);
  const clientTimestamp = normalizeClientTimestamp(input, errors);
  const source = getOptionalString(input, 'source', errors) as EventSource | undefined;

  if (errors.length > 0) {
    return { ok: false, errors };
  }

  return {
    ok: true,
    event: {
      event_id: eventId,
      session_id: sessionId,
      agent_type: agentType,
      event_type: eventType,
      tool_name: toolName,
      status,
      tokens_in: tokensIn,
      tokens_out: tokensOut,
      branch,
      project,
      duration_ms: durationMs,
      metadata: input.metadata ?? {},
      client_timestamp: clientTimestamp,
      model,
      cost_usd: costUsd,
      cache_read_tokens: cacheReadTokens,
      cache_write_tokens: cacheWriteTokens,
      source,
    },
  };
}
