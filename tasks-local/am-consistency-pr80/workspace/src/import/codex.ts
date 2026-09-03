import fs from 'fs';
import path from 'path';
import os from 'os';
import crypto from 'crypto';
import type { NormalizedIngestEvent } from '../contracts/event-contract.js';
import { pricingRegistry } from '../pricing/index.js';
import { parsePatchMeta } from '../otel/parser.js';
import { discoverJsonlFilesRecursive } from '../util/file-discovery.js';
import { codexInvocationMode } from '../util/invocation-mode.js';

// ─── Codex JSONL line types ─────────────────────────────────────────────

interface CodexTokenUsage {
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  reasoning_output_tokens?: number;
  total_tokens?: number;
}


interface CodexLogLine {
  timestamp: string;
  type: string;
  payload: {
    // session_meta
    id?: string;
    cwd?: string;
    originator?: string;
    timestamp?: string;
    model?: string;

    // event_msg
    type?: string;
    info?: {
      total_token_usage?: CodexTokenUsage;
      last_token_usage?: CodexTokenUsage;
      model_context_window?: number;
    };

    // response_item
    role?: string;
    content?: Array<{ type: string; text?: string }>;

    // generic fields from OTEL-style
    [key: string]: unknown;
  };
}

// ─── Discover JSONL files ──────────────────────────────────────────────

export function discoverCodexLogs(
  baseDir?: string,
  options: { excludePatterns?: string[] } = {},
): string[] {
  const codexHome = baseDir ?? process.env.CODEX_HOME ?? path.join(os.homedir(), '.codex');
  const sessionsDir = path.join(codexHome, 'sessions');
  return discoverJsonlFilesRecursive(sessionsDir, { excludePatterns: options.excludePatterns });
}

// ─── Read model from config.toml ────────────────────────────────────────

function readCodexModel(codexHome?: string): string | undefined {
  const base = codexHome ?? process.env.CODEX_HOME ?? path.join(os.homedir(), '.codex');
  const configPath = path.join(base, 'config.toml');
  try {
    const content = fs.readFileSync(configPath, 'utf-8');
    // Simple parse for top-level model = "..."
    const match = content.match(/^model\s*=\s*"([^"]+)"/m);
    return match?.[1];
  } catch {
    return undefined;
  }
}

// ─── Parse a single JSONL file ──────────────────────────────────────────

export function parseCodexFile(
  filePath: string,
  options?: { from?: Date; to?: Date; codexDir?: string },
): NormalizedIngestEvent[] {
  const events: NormalizedIngestEvent[] = [];
  const content = fs.readFileSync(filePath, 'utf-8');
  const lines = content.split('\n').filter(l => l.trim());

  const defaultModel = readCodexModel(options?.codexDir);

  // First pass: extract session metadata
  let sessionId: string | undefined;
  let cwd: string | undefined;
  let sessionTimestamp: string | undefined;
  let originator: string | undefined;
  let firstTurnModel: string | undefined;

  for (const rawLine of lines) {
    let line: CodexLogLine;
    try {
      line = JSON.parse(rawLine) as CodexLogLine;
    } catch {
      continue;
    }

    if (line.type === 'session_meta' && !sessionId) {
      sessionId = line.payload.id;
      cwd = line.payload.cwd;
      sessionTimestamp = line.payload.timestamp ?? line.timestamp;
      originator = line.payload.originator;
    }
    if (line.type === 'turn_context' && !firstTurnModel && typeof line.payload.model === 'string') {
      firstTurnModel = line.payload.model;
    }
  }

  const mode = codexInvocationMode(originator);

  // Fall back to filename for session ID
  if (!sessionId) {
    const basename = path.basename(filePath, '.jsonl');
    // Extract UUID from filename like "rollout-2026-02-18T20-10-57-019c7373-39f7..."
    const uuidMatch = basename.match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
    sessionId = uuidMatch?.[1] ?? basename;
  }

  const project = cwd ? path.basename(cwd) : undefined;

  // Apply date filter on session start time
  if (sessionTimestamp && options?.from) {
    const ts = new Date(sessionTimestamp);
    if (ts < options.from) return events;
  }
  if (sessionTimestamp && options?.to) {
    const ts = new Date(sessionTimestamp);
    if (ts > options.to) return events;
  }

  // Second pass: extract events
  let prevTokensIn = 0;
  let prevTokensOut = 0;
  let prevCacheRead = 0;
  let eventIndex = 0;
  let currentModel = defaultModel;
  let currentModelSource: 'turn_context' | 'config' | undefined = defaultModel ? 'config' : undefined;

  for (const rawLine of lines) {
    let line: CodexLogLine;
    try {
      line = JSON.parse(rawLine) as CodexLogLine;
    } catch {
      continue;
    }

    // Generate session_start from session_meta
    if (line.type === 'session_meta') {
      const eventId = crypto
        .createHash('sha256')
        .update(`codex:${sessionId}:meta`)
        .digest('hex')
        .slice(0, 32);

      events.push({
        event_id: `import-cdx-${eventId}`,
        session_id: sessionId,
        agent_type: 'codex',
        event_type: 'session_start',
        status: 'success',
        tokens_in: 0,
        tokens_out: 0,
        model: firstTurnModel ?? defaultModel,
        project,
        client_timestamp: line.timestamp,
        metadata: {
          originator,
          cwd,
          _model_source: firstTurnModel ? 'turn_context' : currentModelSource,
        },
        source: 'import',
      });
      continue;
    }

    // Codex records the model selected for each turn in the JSONL. This is the
    // authoritative source for usage attribution; config.toml is only a legacy
    // fallback for older logs that predate turn_context.
    if (line.type === 'turn_context' && typeof line.payload?.model === 'string') {
      currentModel = line.payload.model;
      currentModelSource = 'turn_context';
      continue;
    }

    // Extract token deltas from token_count events
    if (line.type === 'event_msg' && line.payload?.type === 'token_count') {
      const usage = line.payload.info?.total_token_usage;
      if (!usage) continue;

      const totalIn = (usage.input_tokens ?? 0);
      const totalOut = (usage.output_tokens ?? 0);
      const totalCacheRead = (usage.cached_input_tokens ?? 0);

      // Compute deltas
      const deltaIn = totalIn - prevTokensIn;
      const deltaOut = totalOut - prevTokensOut;
      const deltaCacheRead = totalCacheRead - prevCacheRead;

      prevTokensIn = totalIn;
      prevTokensOut = totalOut;
      prevCacheRead = totalCacheRead;

      // OpenAI/Codex report input_tokens as cache-inclusive (cached_input_tokens
      // is a subset of it). Bill only the uncached remainder at the full input
      // rate; the cached portion is charged separately at the cache-read rate.
      const deltaInputUncached = Math.max(0, deltaIn - deltaCacheRead);

      // Only emit if there's a meaningful delta
      if (deltaIn <= 0 && deltaOut <= 0) continue;

      const eventId = crypto
        .createHash('sha256')
        .update(`codex:${sessionId}:token:${eventIndex}`)
        .digest('hex')
        .slice(0, 32);

      // Calculate cost from deltas
      const costUsd = currentModel
        ? pricingRegistry.calculate(currentModel, {
            input: deltaInputUncached,
            output: deltaOut,
            cacheRead: deltaCacheRead,
          })
        : undefined;

      events.push({
        event_id: `import-cdx-${eventId}`,
        session_id: sessionId,
        agent_type: 'codex',
        event_type: 'llm_response',
        status: 'success',
        tokens_in: deltaInputUncached,
        tokens_out: deltaOut,
        cache_read_tokens: deltaCacheRead,
        model: currentModel,
        cost_usd: costUsd ?? undefined,
        project,
        client_timestamp: line.timestamp,
        metadata: {
          _synthetic: true,
          _source: 'codex_session_jsonl',
          _model_source: currentModelSource,
        },
        source: 'import',
      });

      eventIndex++;
      continue;
    }

    // Extract file stats from apply_patch tool calls
    // Format 1: custom_tool_call with name=apply_patch and input=<patch>
    // Format 2: function_call with name=exec_command and arguments={"cmd":"apply_patch <<'PATCH'\n..."}
    if (line.type === 'response_item') {
      const payload = line.payload as Record<string, unknown>;
      let patchContent: string | undefined;

      if (payload.name === 'apply_patch' && typeof payload.input === 'string') {
        patchContent = payload.input;
      } else if (payload.name === 'exec_command' && typeof payload.arguments === 'string') {
        try {
          const args = JSON.parse(payload.arguments) as { cmd?: string };
          if (args.cmd && args.cmd.startsWith('apply_patch')) {
            patchContent = args.cmd;
          }
        } catch {
          // arguments might be a plain string containing apply_patch
          if (payload.arguments.startsWith('apply_patch') || payload.arguments.includes('*** Begin Patch')) {
            patchContent = payload.arguments;
          }
        }
      }

      if (patchContent) {
        const patchMeta = parsePatchMeta(patchContent);
        if (patchMeta) {
          const eventId = crypto
            .createHash('sha256')
            .update(`codex:${sessionId}:patch:${eventIndex}`)
            .digest('hex')
            .slice(0, 32);

          events.push({
            event_id: `import-cdx-${eventId}`,
            session_id: sessionId,
            agent_type: 'codex',
            event_type: 'tool_use',
            tool_name: 'apply_patch',
            status: 'success',
            tokens_in: 0,
            tokens_out: 0,
            project,
            client_timestamp: line.timestamp,
            metadata: {
              file_path: patchMeta.file_path,
              lines_added: patchMeta.lines_added,
              lines_removed: patchMeta.lines_removed,
            },
            source: 'import',
          });

          eventIndex++;
        }
        continue;
      }
    }

    // Skip other event types for now
  }

  // Add session_end event
  if (events.length > 0) {
    const lastTimestamp = lines.length > 0
      ? (() => { try { return (JSON.parse(lines[lines.length - 1]) as CodexLogLine).timestamp; } catch { return undefined; } })()
      : undefined;

    const eventId = crypto
      .createHash('sha256')
      .update(`codex:${sessionId}:end`)
      .digest('hex')
      .slice(0, 32);

    events.push({
      event_id: `import-cdx-${eventId}`,
      session_id: sessionId,
      agent_type: 'codex',
      event_type: 'session_end',
      status: 'success',
      tokens_in: 0,
      tokens_out: 0,
      model: currentModel,
      project,
      client_timestamp: lastTimestamp,
      metadata: {
        total_tokens_in: prevTokensIn,
        total_tokens_out: prevTokensOut,
        total_cache_read: prevCacheRead,
        _model_source: currentModelSource,
      },
      source: 'import',
    });
  }

  // Invocation mode is a session-level constant derived from originator; stamp it
  // on every event so the session upsert persists it regardless of insert order.
  return mode ? events.map((e) => ({ ...e, mode })) : events;
}

// ─── File hash for import state tracking ────────────────────────────────

export function hashFile(filePath: string): string {
  const content = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(content).digest('hex');
}
