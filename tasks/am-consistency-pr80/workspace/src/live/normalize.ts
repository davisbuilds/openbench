export type LiveItemKind =
  | 'user_message'
  | 'assistant_message'
  | 'reasoning'
  | 'tool_call'
  | 'tool_result'
  | 'command_execution'
  | 'file_change'
  | 'plan_update'
  | 'diff_snapshot'
  | 'status_change';

export interface CanonicalLiveItem {
  kind: LiveItemKind;
  status?: string;
  created_at?: string;
  source_item_id?: string;
  payload: Record<string, unknown>;
}

export interface ClaudeLiveBlock {
  type: string;
  text?: string;
  thinking?: string;
  id?: string;
  name?: string;
  input?: unknown;
  content?: string;
  is_error?: boolean;
  tool_use_id?: string;
}

export interface LivePlanStep {
  id?: string;
  label: string;
  status?: string;
}

export interface CanonicalLivePlanState {
  summary?: string;
  steps: LivePlanStep[];
}

export interface LivePrivacyPolicy {
  capturePrompts: boolean;
  captureReasoning: boolean;
  captureToolArguments: boolean;
  diffPayloadMaxBytes: number;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function utf8Size(value: string): number {
  return new TextEncoder().encode(value).length;
}

function truncateUtf8(value: string, maxBytes: number): string {
  if (maxBytes <= 0) return '';
  let end = value.length;
  while (end > 0 && utf8Size(value.slice(0, end)) > maxBytes) {
    end -= 1;
  }
  return value.slice(0, end);
}

export function applyLivePrivacyPolicy(
  item: CanonicalLiveItem,
  policy: LivePrivacyPolicy,
): CanonicalLiveItem {
  if (item.kind === 'user_message' && !policy.capturePrompts) {
    return {
      ...item,
      payload: {
        redacted: true,
        reason: 'prompt_capture_disabled',
      },
    };
  }

  if (item.kind === 'reasoning' && !policy.captureReasoning) {
    return {
      ...item,
      payload: {
        redacted: true,
        reason: 'reasoning_capture_disabled',
      },
    };
  }

  if (item.kind === 'tool_call' && !policy.captureToolArguments) {
    return {
      ...item,
      payload: {
        ...item.payload,
        input: { redacted: true },
        input_redacted: true,
      },
    };
  }

  if (item.kind === 'diff_snapshot' && policy.diffPayloadMaxBytes > 0) {
    const encoded = JSON.stringify(item.payload);
    if (utf8Size(encoded) <= policy.diffPayloadMaxBytes) return item;

    const preview = truncateUtf8(encoded, policy.diffPayloadMaxBytes);
    return {
      ...item,
      payload: {
        truncated: true,
        reason: 'diff_payload_cap_exceeded',
        original_size_bytes: utf8Size(encoded),
        preview_json: preview,
      },
    };
  }

  return item;
}

export function normalizeClaudeBlock(
  role: 'user' | 'assistant',
  block: ClaudeLiveBlock,
  createdAt?: string,
): CanonicalLiveItem | null {
  switch (block.type) {
    case 'text':
      return {
        kind: role === 'user' ? 'user_message' : 'assistant_message',
        created_at: createdAt,
        payload: { text: block.text ?? '' },
      };
    case 'thinking':
      return {
        kind: 'reasoning',
        created_at: createdAt,
        payload: { text: block.thinking ?? '' },
      };
    case 'tool_use':
      return {
        kind: 'tool_call',
        created_at: createdAt,
        source_item_id: block.id,
        payload: {
          tool_name: block.name ?? 'unknown',
          input: block.input ?? null,
        },
      };
    case 'tool_result':
      return {
        kind: 'tool_result',
        status: block.is_error ? 'error' : 'success',
        created_at: createdAt,
        source_item_id: block.tool_use_id,
        payload: {
          content: block.content ?? '',
          is_error: Boolean(block.is_error),
        },
      };
    default:
      return null;
  }
}

export function normalizeCodexItem(input: {
  type: string;
  created_at?: string;
  id?: string;
  status?: string;
  payload?: Record<string, unknown>;
}): CanonicalLiveItem | null {
  const payload = input.payload ?? {};

  switch (input.type) {
    case 'commandExecution':
      return {
        kind: 'command_execution',
        status: input.status ?? (typeof payload.status === 'string' ? payload.status : undefined),
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'fileChange':
      return {
        kind: 'file_change',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'mcpToolCall':
      return {
        kind: 'tool_call',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'tool_result':
      return {
        kind: 'tool_result',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'plan':
    case 'turn/plan/updated':
      return {
        kind: 'plan_update',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'diff':
    case 'turn/diff/updated':
      return {
        kind: 'diff_snapshot',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'reasoning':
      return {
        kind: 'reasoning',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'assistant_message':
      return {
        kind: 'assistant_message',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'user_message':
      return {
        kind: 'user_message',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    case 'status':
      return {
        kind: 'status_change',
        status: input.status,
        created_at: input.created_at,
        source_item_id: input.id,
        payload,
      };
    default:
      return null;
  }
}

export function normalizePlanState(value: unknown): CanonicalLivePlanState {
  const record = asRecord(value);
  const rawSteps = Array.isArray(record?.steps) ? record.steps : [];
  const steps: LivePlanStep[] = [];

  for (const rawStep of rawSteps) {
    const step = asRecord(rawStep);
    if (!step) continue;
    const label = typeof step.label === 'string'
      ? step.label
      : typeof step.step === 'string'
        ? step.step
        : typeof step.title === 'string'
          ? step.title
          : '';
    if (!label) continue;
    steps.push({
      id: typeof step.id === 'string' ? step.id : undefined,
      label,
      status: typeof step.status === 'string' ? step.status : undefined,
    });
  }

  return {
    summary: typeof record?.summary === 'string' ? record.summary : undefined,
    steps,
  };
}
