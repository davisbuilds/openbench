// Dependency-light protobuf wire reader for Antigravity conversation blobs.
//
// Scope: decode the descriptor-pinned surfaces only — the gemini_coder.Step
// envelope and the Google UsageMetadata schema (see fieldmap.ts + the baseline
// doc). Private exa.cortex_pb payload internals are navigated generically (by
// presence), not interpreted here.
//
// The reader never throws on malformed input: a truncated/garbage tail stops
// parsing and returns what was read so far (real conversation DBs can hold
// partially-written rows for a live session).

import {
  STEP_FIELDS,
  STEP_PAYLOAD_KINDS,
  USAGE_METADATA_FIELDS,
  GEN_METADATA_WRAPPER_FIELD,
  GENERATOR_METADATA_FIELDS,
  CORTEX_USAGE_FIELDS,
} from './fieldmap.js';

export type WireType = 0 | 1 | 2 | 5;

export interface ProtoField {
  wire: WireType;
  /** present for wire type 0 (varint) */
  value?: number;
  /** present for wire type 2 (length-delimited) */
  bytes?: Buffer;
}

/** Read a base-128 varint as a JS number (safe for the <=2^53 counts we decode). */
function readVarint(buf: Buffer, pos: number): [number, number] {
  let result = 0;
  let shift = 0;
  let p = pos;
  for (;;) {
    if (p >= buf.length) throw new RangeError('varint truncated');
    const byte = buf[p++];
    // multiply instead of shift so bits above 31 don't overflow int32
    result += (byte & 0x7f) * 2 ** shift;
    if ((byte & 0x80) === 0) break;
    shift += 7;
    if (shift > 63) throw new RangeError('varint too long');
  }
  return [result, p];
}

/** Decode a protobuf message into a map of field number -> occurrences. */
export function decodeMessage(buf: Buffer): Map<number, ProtoField[]> {
  const fields = new Map<number, ProtoField[]>();
  let pos = 0;
  while (pos < buf.length) {
    let key: number;
    try {
      [key, pos] = readVarint(buf, pos);
    } catch {
      break;
    }
    const field = Math.floor(key / 8);
    const wire = key & 7;
    let entry: ProtoField;
    if (wire === 0) {
      let value: number;
      try {
        [value, pos] = readVarint(buf, pos);
      } catch {
        break;
      }
      entry = { wire, value };
    } else if (wire === 2) {
      let len: number;
      try {
        [len, pos] = readVarint(buf, pos);
      } catch {
        break;
      }
      if (len < 0 || pos + len > buf.length) break;
      entry = { wire, bytes: buf.subarray(pos, pos + len) };
      pos += len;
    } else if (wire === 5) {
      pos += 4;
      entry = { wire };
    } else if (wire === 1) {
      pos += 8;
      entry = { wire };
    } else {
      break; // groups (3/4) unsupported / reserved wire types → stop
    }
    const list = fields.get(field);
    if (list) list.push(entry);
    else fields.set(field, [entry]);
  }
  return fields;
}

/** First varint value for a field, or undefined. */
export function getVarint(
  fields: Map<number, ProtoField[]>,
  field: number,
): number | undefined {
  return fields.get(field)?.[0]?.value;
}

/** First length-delimited payload for a field, or undefined. */
function getBytes(
  fields: Map<number, ProtoField[]>,
  field: number,
): Buffer | undefined {
  return fields.get(field)?.[0]?.bytes;
}

export interface StepEnvelope {
  /** CortexStepType enum value (its own numbering; NOT the oneof kind field number). */
  type?: number;
  /** CortexStepStatus enum value. */
  status?: number;
  /** Snake-case step kind, from whichever Step `oneof` payload field is present. */
  kind?: string;
  hasMetadata: boolean;
  hasError: boolean;
}

/**
 * Decode a `steps.step_payload` blob, which is the FULL serialized gemini_coder.Step
 * (top-level fields `{1: type, 4: status, 5: metadata, <oneof>: payload}`), not just
 * the oneof payload. `kind` is derived from the present oneof field — deliberately
 * NOT from `type` (the CortexStepType enum uses different numbers). The per-kind
 * CortexStep* payload internals are private and left opaque.
 */
export function decodeStepEnvelope(stepPayload: Buffer): StepEnvelope {
  const f = decodeMessage(stepPayload);
  let kind: string | undefined;
  for (const [num, name] of Object.entries(STEP_PAYLOAD_KINDS)) {
    if (f.has(Number(num))) {
      kind = name;
      break;
    }
  }
  return {
    type: getVarint(f, STEP_FIELDS.type),
    status: getVarint(f, STEP_FIELDS.status),
    kind,
    hasMetadata: f.has(STEP_FIELDS.metadata),
    hasError: f.has(STEP_FIELDS.error),
  };
}

/**
 * Extract a step's timestamp from its `steps.metadata` (CortexStepMetadata) blob:
 * `metadata.field1.field1` is a unix-seconds created-at. Returns epoch ms, or
 * undefined if absent/out of a sane range. (Verified across real rows.)
 */
export function decodeStepTimestampMs(metadata: Buffer): number | undefined {
  const inner = getBytes(decodeMessage(metadata), 1);
  if (!inner) return undefined;
  const secs = getVarint(decodeMessage(inner), 1);
  if (secs === undefined || secs < 1_000_000_000 || secs > 4_000_000_000) return undefined;
  return secs * 1000;
}

export interface GoogleUsage {
  promptTokenCount: number;
  candidatesTokenCount: number;
  totalTokenCount: number;
  cachedContentTokenCount: number;
  toolUsePromptTokenCount: number;
  thoughtsTokenCount: number;
}

/**
 * Decode a google.cloud.aiplatform.master.UsageMetadata message.
 *
 * NOTE: in shipped Antigravity conversation DBs this schema appears as the
 * request generation *config* (e.g. max_output_tokens), not the persisted
 * response usage — the real per-turn accounting is the private
 * CortexGeneratorMetadata message (fields num_input_tokens / num_output_tokens /
 * thinking_output_tokens), whose field numbers are not yet descriptor-pinned.
 * See the baseline doc "Real token accounting" section. This decoder is kept
 * because the schema is authoritative; wiring the cost-bearing source is Task 3.
 */
export function decodeGoogleUsage(bytes: Buffer): GoogleUsage {
  const f = decodeMessage(bytes);
  return {
    promptTokenCount: getVarint(f, USAGE_METADATA_FIELDS.promptTokenCount) ?? 0,
    candidatesTokenCount: getVarint(f, USAGE_METADATA_FIELDS.candidatesTokenCount) ?? 0,
    totalTokenCount: getVarint(f, USAGE_METADATA_FIELDS.totalTokenCount) ?? 0,
    cachedContentTokenCount: getVarint(f, USAGE_METADATA_FIELDS.cachedContentTokenCount) ?? 0,
    toolUsePromptTokenCount: getVarint(f, USAGE_METADATA_FIELDS.toolUsePromptTokenCount) ?? 0,
    thoughtsTokenCount: getVarint(f, USAGE_METADATA_FIELDS.thoughtsTokenCount) ?? 0,
  };
}

export interface CortexUsage {
  systemPromptTokens: number;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number;
  thinkingTokens: number;
  answerTokens: number;
}

export interface GeneratorMetadata {
  /** Model id, e.g. "gemini-pro-default" (falls back to the display string). */
  model?: string;
  usage?: CortexUsage;
}

/**
 * Decode a `gen_metadata.data` blob: the real per-generation record. Navigates the
 * wrapper → CortexGeneratorMetadata → CortexUsage using the empirically-pinned
 * field numbers (see fieldmap.ts + baseline doc). This is the cost-bearing source.
 */
export function decodeGeneratorMetadata(blob: Buffer): GeneratorMetadata {
  const wrapper = decodeMessage(blob);
  const gmBytes = getBytes(wrapper, GEN_METADATA_WRAPPER_FIELD);
  if (!gmBytes) return {};
  const gm = decodeMessage(gmBytes);

  const modelBytes =
    getBytes(gm, GENERATOR_METADATA_FIELDS.model) ??
    getBytes(gm, GENERATOR_METADATA_FIELDS.modelDisplay);
  const model = modelBytes ? modelBytes.toString('utf-8') : undefined;

  const usageBytes = getBytes(gm, GENERATOR_METADATA_FIELDS.usage);
  let usage: CortexUsage | undefined;
  if (usageBytes) {
    const u = decodeMessage(usageBytes);
    usage = {
      systemPromptTokens: getVarint(u, CORTEX_USAGE_FIELDS.systemPromptTokens) ?? 0,
      inputTokens: getVarint(u, CORTEX_USAGE_FIELDS.inputTokens) ?? 0,
      outputTokens: getVarint(u, CORTEX_USAGE_FIELDS.outputTokens) ?? 0,
      cachedTokens: getVarint(u, CORTEX_USAGE_FIELDS.cachedTokens) ?? 0,
      thinkingTokens: getVarint(u, CORTEX_USAGE_FIELDS.thinkingTokens) ?? 0,
      answerTokens: getVarint(u, CORTEX_USAGE_FIELDS.answerTokens) ?? 0,
    };
  }
  return { model, usage };
}

export interface BillingTokens {
  /** Uncached, full-rate prompt tokens (system + non-cached input). */
  tokensIn: number;
  /** Total output tokens (includes reasoning). */
  tokensOut: number;
  /** Cached prompt tokens (billed at the cache-read rate). */
  cacheReadTokens: number;
  /** Reasoning tokens (informational lane; already inside tokensOut). */
  thoughtsTokens: number;
}

/**
 * Map CortexUsage onto AgentMonitor's token buckets, honoring the cache-inclusive
 * invariant: tokensIn (uncached) and cacheReadTokens (cached) are additive and
 * never overlap. system + input are separate CortexUsage buckets (a distinct
 * num_system_prompt_tokens field exists), so both count as full-rate input.
 */
export function deriveBillingTokens(u: CortexUsage): BillingTokens {
  return {
    tokensIn: u.systemPromptTokens + u.inputTokens,
    tokensOut: u.outputTokens,
    cacheReadTokens: u.cachedTokens,
    thoughtsTokens: u.thinkingTokens,
  };
}
