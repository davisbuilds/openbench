import fs from 'fs';
import path from 'path';
import os from 'os';
import crypto from 'crypto';
import type { NormalizedIngestEvent, EventType } from '../contracts/event-contract.js';
import { discoverJsonlFilesRecursive } from '../util/file-discovery.js';
import { claudeInvocationMode } from '../util/invocation-mode.js';

// ─── Claude Code JSONL line types ──────────────────────────────────────

interface ClaudeCodeUsage {
  input_tokens?: number;
  output_tokens?: number;
  cache_creation_input_tokens?: number;
  cache_read_input_tokens?: number;
}

interface ClaudeCodeMessage {
  model?: string;
  usage?: ClaudeCodeUsage;
  content?: unknown;
  stop_reason?: string;
}

interface ClaudeCodeLogLine {
  type?: string;
  sessionId?: string;
  model?: string;
  costUSD?: number;
  usage?: ClaudeCodeUsage;
  message?: ClaudeCodeMessage;  // assistant lines nest model/usage here
  timestamp?: string;
  name?: string;          // tool name for tool_use lines
  tool_name?: string;     // alternate field
  content?: unknown;
  duration_ms?: number;
  durationMs?: number;
  error?: string | { message?: string };
  cwd?: string;
  gitBranch?: string;
  entrypoint?: string;    // 'cli' (interactive) | 'sdk-cli' (headless -p)
  promptSource?: string;  // 'typed'/'queued'/... (interactive) | 'sdk' (headless)
  // tool_result fields
  is_error?: boolean;
  status?: string;
  // tool_use input
  input?: unknown;
  // tool_result output
  output?: unknown;
}

// ─── Event type mapping ─────────────────────────────────────────────────

const TYPE_MAP: Record<string, EventType> = {
  tool_use: 'tool_use',
  tool_result: 'tool_use',
  assistant: 'llm_response',
  error: 'error',
  session_start: 'session_start',
  session_end: 'session_end',
};

// ─── Discover JSONL files ──────────────────────────────────────────────

export function discoverClaudeCodeLogs(
  baseDir?: string,
  options: { excludePatterns?: string[] } = {},
): string[] {
  const claudeDir = baseDir ?? path.join(os.homedir(), '.claude');
  const projectsDir = path.join(claudeDir, 'projects');
  return discoverJsonlFilesRecursive(projectsDir, { excludePatterns: options.excludePatterns });
}

// ─── Parse a single JSONL file ──────────────────────────────────────────

export function parseClaudeCodeFile(
  filePath: string,
  options?: { from?: Date; to?: Date },
): NormalizedIngestEvent[] {
  const events: NormalizedIngestEvent[] = [];
  const content = fs.readFileSync(filePath, 'utf-8');
  const lines = content.split('\n').filter(l => l.trim());

  // Extract session ID from filename (session UUID) or from first line
  const fileBasename = path.basename(filePath, '.jsonl');

  // Track cumulative cost for delta calculation
  let prevCostUSD = 0;

  for (let i = 0; i < lines.length; i++) {
    let line: ClaudeCodeLogLine;
    try {
      line = JSON.parse(lines[i]) as ClaudeCodeLogLine;
    } catch {
      continue; // Skip malformed lines
    }

    if (!line.type) continue;

    const sessionId = line.sessionId ?? fileBasename;

    // Apply date filter
    if (line.timestamp && options?.from) {
      const ts = new Date(line.timestamp);
      if (ts < options.from) continue;
    }
    if (line.timestamp && options?.to) {
      const ts = new Date(line.timestamp);
      if (ts > options.to) continue;
    }

    const eventType = TYPE_MAP[line.type] ?? 'response';

    // Extract tool name
    const toolName = line.name ?? line.tool_name;

    // Resolve model and usage — check top-level first, then nested message object
    const msg = line.message;
    const model = line.model ?? msg?.model;
    const usage = line.usage ?? msg?.usage;

    // Compute delta cost from cumulative costUSD
    let costDelta: number | undefined;
    if (typeof line.costUSD === 'number' && line.costUSD > 0) {
      costDelta = line.costUSD - prevCostUSD;
      if (costDelta < 0) costDelta = 0; // Safety: shouldn't happen
      prevCostUSD = line.costUSD;
    }

    // Extract token counts
    const tokensIn = usage?.input_tokens ?? 0;
    const tokensOut = usage?.output_tokens ?? 0;
    const cacheRead = usage?.cache_read_input_tokens ?? 0;
    const cacheWrite = usage?.cache_creation_input_tokens ?? 0;

    // Extract project (basename of cwd) and branch
    const project = line.cwd ? path.basename(line.cwd) : undefined;
    const branch = line.gitBranch;

    // Determine status
    let status: 'success' | 'error' | 'timeout' = 'success';
    if (line.type === 'error' || line.is_error || line.status === 'error') {
      status = 'error';
    }

    // Deterministic event_id for dedup on re-import
    const eventId = crypto
      .createHash('sha256')
      .update(`claude-code:${sessionId}:${i}`)
      .digest('hex')
      .slice(0, 32);

    // Build metadata with content for transcript enrichment
    const metadataObj: Record<string, unknown> = {};
    if (typeof line.error === 'string') metadataObj.error = line.error;
    else if (line.error?.message) metadataObj.error = line.error.message;

    // Extract content preview from various formats
    if (line.content !== undefined) {
      if (typeof line.content === 'string') {
        metadataObj.content_preview = line.content.slice(0, 500);
      } else if (Array.isArray(line.content)) {
        // Claude Code uses content blocks: [{type: "text", text: "..."}]
        const textParts: string[] = [];
        for (const block of line.content) {
          if (block && typeof block === 'object' && 'text' in block && typeof block.text === 'string') {
            textParts.push(block.text);
          }
        }
        if (textParts.length > 0) {
          metadataObj.content_preview = textParts.join('\n').slice(0, 500);
        }
      }
    }

    // Capture tool input/output for transcript enrichment
    if (line.type === 'tool_use' && line.input) {
      if (typeof line.input === 'object' && line.input !== null) {
        const inp = line.input as Record<string, unknown>;
        if (inp.command) metadataObj.command = String(inp.command).slice(0, 200);
        if (inp.file_path) metadataObj.file_path = String(inp.file_path);
        if (inp.pattern) metadataObj.pattern = String(inp.pattern);
        if (inp.query) metadataObj.query = String(inp.query);

        // Compute lines added/removed for edit tools
        const tn = toolName ?? '';
        if (tn === 'Edit' || tn === 'MultiEdit') {
          if (typeof inp.old_string === 'string' && inp.old_string) {
            metadataObj.lines_removed = inp.old_string.split('\n').length;
          }
          if (typeof inp.new_string === 'string' && inp.new_string) {
            metadataObj.lines_added = inp.new_string.split('\n').length;
          }
        } else if (tn === 'Write') {
          if (typeof inp.content === 'string' && inp.content) {
            metadataObj.lines_added = inp.content.split('\n').length;
          }
        }
      }
    }
    if (line.type === 'tool_result' && line.output !== undefined) {
      const outputStr = typeof line.output === 'string' ? line.output : JSON.stringify(line.output);
      metadataObj.content_preview = outputStr.slice(0, 500);
    }

    const event: NormalizedIngestEvent = {
      event_id: `import-cc-${eventId}`,
      session_id: sessionId,
      agent_type: 'claude_code',
      event_type: eventType,
      tool_name: eventType === 'tool_use' ? toolName : undefined,
      status,
      tokens_in: tokensIn,
      tokens_out: tokensOut,
      cache_read_tokens: cacheRead,
      cache_write_tokens: cacheWrite,
      model,
      cost_usd: costDelta && costDelta > 0 ? costDelta : undefined,
      duration_ms: line.duration_ms ?? line.durationMs,
      project,
      branch,
      client_timestamp: line.timestamp,
      metadata: Object.keys(metadataObj).length > 0 ? metadataObj : {},
      source: 'import',
      mode: claudeInvocationMode(line.entrypoint, line.promptSource),
    };

    events.push(event);
  }

  return events;
}

// ─── File hash for import state tracking ────────────────────────────────

export function hashFile(filePath: string): string {
  const content = fs.readFileSync(filePath);
  return crypto.createHash('sha256').update(content).digest('hex');
}
