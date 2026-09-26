// Historical importer for Antigravity CLI conversation databases.
//
// Each `~/.gemini/antigravity-cli/conversations/<uuid>.db` is a SQLite file whose
// `steps`/`gen_metadata` blobs are plaintext protobuf (see
// docs/specs/baselines/antigravity-proto-fieldmap.md). This projects one session
// per DB into normalized events: session_start, per-step activity, and one
// llm_response per generation carrying usage + cost.

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import Database from 'better-sqlite3';
import type { NormalizedIngestEvent, EventType } from '../contracts/event-contract.js';
import { discoverDbFilesRecursive } from '../util/file-discovery.js';
import { pricingRegistry } from '../pricing/index.js';
import {
  decodeStepEnvelope,
  decodeGeneratorMetadata,
  deriveBillingTokens,
  decodeStepTimestampMs,
} from './antigravity/proto.js';

const AGENT_TYPE = 'antigravity';

function antigravityHome(dir?: string): string {
  return dir ?? path.join(os.homedir(), '.gemini', 'antigravity-cli');
}

export function discoverAntigravityLogs(
  dir?: string,
  options?: { excludePatterns?: string[] },
): string[] {
  const root = path.join(antigravityHome(dir), 'conversations');
  // Recurse: conversation DBs may be nested under project/date subdirectories,
  // matching the spec's `conversations/**/*.db` scope (mirrors the Claude/Codex
  // JSONL importers). A flat readdir would silently drop nested sessions.
  return discoverDbFilesRecursive(root, { excludePatterns: options?.excludePatterns });
}

export function hashFile(filePath: string): string {
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex');
}

// --- step kind → event taxonomy ---------------------------------------------

const USER_PROMPT_KINDS = new Set(['user_input', 'plan_input', 'ask_question']);
const RESPONSE_KINDS = new Set([
  'planner_response', 'suggested_responses', 'notify_user', 'ephemeral_message',
  'system_message', 'conversation_history', 'critique', 'findings',
  'proposal_feedback', 'manager_feedback', 'code_acknowledgement',
]);
const FILE_CHANGE_KINDS = new Set([
  'write_to_file', 'file_change', 'code_action', 'propose_code', 'edit_notebook',
  'write_blob', 'move', 'delete_directory',
]);
const TOOL_KIND_PREFIXES = ['browser_', 'mcp_', 'cloudsql_', 'blaze_', 'capture_'];
const TOOL_KINDS = new Set([
  'run_command', 'command_status', 'send_command_input', 'shell_exec', 'grep_search',
  'code_search', 'internal_search', 'search_web', 'search_knowledge_base',
  'lookup_knowledge_base', 'trajectory_search', 'find', 'find_all_references',
  'view_file', 'view_file_outline', 'view_code_item', 'view_content_chunk',
  'list_directory', 'list_resources', 'read_resource', 'read_url_content',
  'read_terminal', 'read_notebook', 'execute_notebook', 'run_extension_code',
  'lint_diff', 'lint_applet', 'compile', 'compile_applet',
  'install_applet_dependencies', 'install_applet_package', 'restart_dev_server',
  'generate_image', 'open_browser_url', 'set_up_firebase', 'deploy_firebase',
  'set_up_cloudsql', 'mcp_tool', 'agency_tool_call', 'invoke_subagent',
  'rpc_action', 'workspace_api', 'retrieve_memory', 'retrieve_content',
]);

/** Map a step kind to an event type + optional tool name. Unmapped → generic `response`. */
export function stepKindToEvent(kind: string | undefined): { type: EventType; toolName?: string } {
  if (!kind) return { type: 'response' };
  if (USER_PROMPT_KINDS.has(kind)) return { type: 'user_prompt' };
  if (kind === 'finish') return { type: 'session_end' };
  if (kind === 'error_message') return { type: 'error' };
  if (kind === 'git_commit') return { type: 'git_commit', toolName: 'git_commit' };
  if (kind === 'checkpoint') return { type: 'plan_step' };
  if (FILE_CHANGE_KINDS.has(kind)) return { type: 'file_change', toolName: kind };
  if (RESPONSE_KINDS.has(kind)) return { type: 'response' };
  if (TOOL_KINDS.has(kind) || TOOL_KIND_PREFIXES.some((p) => kind.startsWith(p))) {
    return { type: 'tool_use', toolName: kind };
  }
  return { type: 'response' }; // typed generic; kind preserved in metadata, never dropped
}

// --- parse ------------------------------------------------------------------

interface StepRow { idx: number; step_type: number; status: number; step_payload: Buffer; metadata: Buffer | null }
interface GenRow { idx: number; data: Buffer }

function safeAll<T>(db: Database.Database, sql: string): T[] {
  try {
    return db.prepare(sql).all() as T[];
  } catch {
    return [];
  }
}

function eventId(sessionId: string, tag: string): string {
  const h = crypto.createHash('sha256').update(`antigravity:${sessionId}:${tag}`).digest('hex').slice(0, 32);
  return `import-agr-${h}`;
}

const iso = (ms: number | undefined): string | undefined => (ms === undefined ? undefined : new Date(ms).toISOString());

export function parseAntigravityFile(
  filePath: string,
  options?: { from?: Date; to?: Date },
): NormalizedIngestEvent[] {
  const events: NormalizedIngestEvent[] = [];
  const sessionId = path.basename(filePath, '.db');

  let db: Database.Database;
  try {
    db = new Database(filePath, { readonly: true, fileMustExist: true });
  } catch {
    return events;
  }

  try {
    const steps = safeAll<StepRow>(db, 'SELECT idx, step_type, status, step_payload, metadata FROM steps ORDER BY idx');
    const genRows = safeAll<GenRow>(db, 'SELECT idx, data FROM gen_metadata ORDER BY idx');
    if (steps.length === 0 && genRows.length === 0) return events;

    const stepTs = steps.map((s) => (s.metadata ? decodeStepTimestampMs(s.metadata) : undefined));
    const firstTs = stepTs.find((t) => t !== undefined);

    // Model + usage come from the generation records.
    const generations = genRows.map((g) => decodeGeneratorMetadata(g.data));
    const sessionModel = generations.find((g) => g.model)?.model;

    // Date-scope on the session's first activity.
    if (firstTs !== undefined) {
      if (options?.from && firstTs < options.from.getTime()) return events;
      if (options?.to && firstTs > options.to.getTime()) return events;
    }

    events.push({
      event_id: eventId(sessionId, 'session_start'),
      session_id: sessionId,
      agent_type: AGENT_TYPE,
      event_type: 'session_start',
      status: 'success',
      tokens_in: 0,
      tokens_out: 0,
      model: sessionModel,
      client_timestamp: iso(firstTs),
      metadata: { _source: 'antigravity_db' },
      source: 'import',
    });

    steps.forEach((s, i) => {
      const env = decodeStepEnvelope(s.step_payload);
      const { type, toolName } = stepKindToEvent(env.kind);
      events.push({
        event_id: eventId(sessionId, `step:${s.idx}`),
        session_id: sessionId,
        agent_type: AGENT_TYPE,
        event_type: type,
        tool_name: toolName,
        status: type === 'error' ? 'error' : 'success',
        tokens_in: 0,
        tokens_out: 0,
        model: sessionModel,
        client_timestamp: iso(stepTs[i]),
        metadata: { _source: 'antigravity_db', kind: env.kind ?? null, step_type: s.step_type },
        source: 'import',
      });
    });

    generations.forEach((gm, i) => {
      if (!gm.usage) return;
      const b = deriveBillingTokens(gm.usage);
      const model = gm.model ?? sessionModel;
      const cost = model
        ? pricingRegistry.calculate(model, { input: b.tokensIn, output: b.tokensOut, cacheRead: b.cacheReadTokens })
        : null;
      events.push({
        event_id: eventId(sessionId, `gen:${i}`),
        session_id: sessionId,
        agent_type: AGENT_TYPE,
        event_type: 'llm_response',
        status: 'success',
        tokens_in: b.tokensIn,
        tokens_out: b.tokensOut,
        cache_read_tokens: b.cacheReadTokens,
        model,
        cost_usd: cost ?? undefined,
        client_timestamp: iso(firstTs),
        metadata: { _source: 'antigravity_db', thoughts_tokens: b.thoughtsTokens },
        source: 'import',
      });
    });
  } finally {
    db.close();
  }

  return events;
}
