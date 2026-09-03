import type Database from 'better-sqlite3';
import { claudeInvocationMode } from '../util/invocation-mode.js';
import { config } from '../config.js';
import { resolveContextWindow } from '../pricing/context-windows.js';
import {
  projectIdentityFromCwd,
  type ParsedSkillContext,
  type SessionContextObservation,
} from '../skills/context-observations.js';
import { extractExplicitSkillName } from '../skills/invocation-detection.js';

// --- Tool category normalization ---

const CATEGORY_MAP: Record<string, string> = {
  Read: 'Read',
  NotebookRead: 'Read',
  Write: 'Write',
  NotebookEdit: 'Write',
  Edit: 'Edit',
  MultiEdit: 'Edit',
  Grep: 'Search',
  Glob: 'Search',
  WebSearch: 'Search',
  WebFetch: 'Search',
  Bash: 'Bash',
  Agent: 'Agent',
  ToolSearch: 'Agent',
  Skill: 'Agent',
  AskUserQuestion: 'Other',
};

export function categorizeToolName(toolName: string): string {
  return CATEGORY_MAP[toolName] ?? 'Other';
}

// --- JSONL line types ---

export interface ContentBlock {
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

interface ClaudeCodeLine {
  type?: string;
  parentUuid?: string | null;
  sessionId?: string;
  isSidechain?: boolean;
  isMeta?: boolean;
  cwd?: string;
  timestamp?: string;
  message?: {
    role?: string;
    content?: unknown;
    model?: string;
    usage?: {
      input_tokens?: number;
      cache_read_input_tokens?: number;
      cache_creation_input_tokens?: number;
      output_tokens?: number;
    };
  };
  // progress / system fields
  data?: Record<string, unknown>;
  subtype?: string;
  entrypoint?: string;
  promptSource?: string;
}

// --- Parsed output types ---

export interface ParsedMessage {
  session_id: string;
  ordinal: number;
  role: string;
  content: string; // JSON-serialized ContentBlock[]
  timestamp: string | null;
  has_thinking: number;
  has_tool_use: number;
  content_length: number;
}

export interface ParsedToolCall {
  session_id: string;
  tool_name: string;
  category: string;
  tool_use_id: string | null;
  input_json: string | null;
  subagent_session_id: string | null;
  message_ordinal: number; // used to link to message after insert
}

export interface ParsedSessionMetadata {
  session_id: string;
  project: string | null;
  agent: string;
  first_message: string | null;
  started_at: string | null;
  ended_at: string | null;
  message_count: number;
  user_message_count: number;
  parent_session_id: string | null;
  relationship_type: string | null;
  // Invocation mode derived from the session log (see src/util/invocation-mode.ts).
  // Undefined when the agent emits no signal (e.g. Antigravity).
  mode?: 'interactive' | 'headless';
  // Context-window occupancy numerator: the most recent assistant turn's prompt
  // size (input + cache_read + cache_creation). Undefined when no usage seen.
  context_used_tokens?: number;
  // Model of the most recent assistant turn (for context-window resolution).
  model?: string;
  // First-party context-window size when the source reports one (Codex
  // model_context_window). Undefined for Claude (resolved by default instead).
  context_window_reported?: number;
}

export interface ParsedSession {
  messages: ParsedMessage[];
  toolCalls: ParsedToolCall[];
  metadata: ParsedSessionMetadata;
  skillContext?: ParsedSkillContext;
}

function cleanPreviewText(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\u001b\[[0-9;]*m/g, '').replace(/\s+/g, ' ').trim();
}

function previewTextFromBlocks(blocks: ContentBlock[], isMeta: boolean): string | null {
  const textBlock = blocks.find((block) => block.type === 'text' && typeof block.text === 'string' && block.text.trim());
  const text = textBlock?.text?.trim();
  if (!text) return null;

  if (isMeta) return null;
  if (text.includes('<local-command-caveat>')) return null;
  if (text.includes('<command-name>')) return null;
  if (text.includes('<local-command-stdout>') || text.includes('<local-command-stderr>')) return null;

  return cleanPreviewText(text).slice(0, 200) || null;
}

// --- Extract project name from file path ---

function projectFromPath(filePath: string): string | null {
  // Path pattern: ~/.claude/projects/-Users-dev-my-project/session.jsonl
  // The directory name encodes the full path with '-' as separator, prefixed with '-'.
  // e.g. "-Users-dg-mac-mini-Dev-agentmonitor" → project is "agentmonitor"
  // e.g. "-Users-dev-my-project" → project is "my-project"
  // Strategy: the encoded dir represents a filesystem path. The last path component
  // (after the last known directory separator) is the project name. We use the cwd
  // from the JSONL if available, but as fallback we decode the directory name.
  const parts = filePath.split('/');
  const projectsIdx = parts.indexOf('projects');
  if (projectsIdx >= 0 && projectsIdx + 1 < parts.length) {
    const encodedDir = parts[projectsIdx + 1];
    // The encoded dir is a path like "-Users-dev-my-project".
    // We need to find where the last path component starts.
    // Claude Code encodes "/" as "-", so we need to figure out which "-" is a separator
    // and which is part of the name. Use a heuristic: split by known path prefixes.
    // Most reliable: look for common path patterns.

    // Try matching known patterns: -Users-<user>-<...>-<project>
    // or -home-<user>-<...>-<project>
    const match = encodedDir.match(/^-(?:Users|home)-[^-]+-(.+)$/);
    if (match) {
      // The remainder after "-Users-<username>-" may contain multiple path segments.
      // The last segment (after the last known directory separator '-' that maps to '/')
      // is the project. But we don't know which '-' are separators vs part of names.
      // Best approach: use the cwd from the JSONL data. As fallback, take everything
      // after the last major directory marker.
      const remainder = match[1];
      // Common pattern: Dev-projectname or Documents-projectname
      // Just take everything after the last known single-component directory
      const knownDirs = ['Dev', 'Documents', 'Projects', 'repos', 'code', 'src', 'work', 'projects', 'workspace', 'git'];
      for (const dir of knownDirs) {
        const dirIdx = remainder.indexOf(dir + '-');
        if (dirIdx >= 0) {
          return remainder.slice(dirIdx + dir.length + 1);
        }
      }
      // Fallback: just return the full remainder
      return remainder;
    }
  }
  return null;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function extractSubagentSessionId(input: unknown): string | null {
  const visited = new Set<unknown>();
  const queue: unknown[] = [input];

  while (queue.length > 0) {
    const current = queue.shift();
    if (current == null) continue;
    if (typeof current === 'string') {
      const trimmed = current.trim();
      if (/^agent-[A-Za-z0-9._-]+$/.test(trimmed)) {
        return trimmed;
      }
      continue;
    }
    if (typeof current !== 'object') continue;
    if (visited.has(current)) continue;
    visited.add(current);

    if (Array.isArray(current)) {
      queue.push(...current);
      continue;
    }

    const record = asRecord(current);
    if (!record) continue;
    for (const [key, value] of Object.entries(record)) {
      if ((key === 'session_id' || key === 'sessionId' || key === 'subagent_id')
        && typeof value === 'string'
        && /^agent-[A-Za-z0-9._-]+$/.test(value.trim())) {
        return value.trim();
      }
      queue.push(value);
    }
  }

  return null;
}

function linkParsedSessionRelationships(
  db: Database.Database,
  sessionId: string,
  subagentSessionIds: string[],
): void {
  if (subagentSessionIds.length > 0) {
    const updateChild = db.prepare(`
      UPDATE browsing_sessions
      SET parent_session_id = ?,
          relationship_type = 'subagent'
      WHERE id = ?
    `);

    for (const childId of subagentSessionIds) {
      updateChild.run(sessionId, childId);
    }
  }

  const parent = db.prepare(`
    SELECT session_id
    FROM tool_calls
    WHERE subagent_session_id = ?
    ORDER BY id DESC
    LIMIT 1
  `).get(sessionId) as { session_id: string } | undefined;

  if (parent) {
    db.prepare(`
      UPDATE browsing_sessions
      SET parent_session_id = ?,
          relationship_type = 'subagent'
      WHERE id = ?
    `).run(parent.session_id, sessionId);
  }
}

// --- Parse JSONL content into structured messages ---

export function parseSessionMessages(
  jsonlContent: string,
  sessionId: string,
  filePath?: string,
): ParsedSession {
  const messages: ParsedMessage[] = [];
  const toolCalls: ParsedToolCall[] = [];
  let firstUserMessage: string | null = null;
  let startedAt: string | null = null;
  let endedAt: string | null = null;
  let userMessageCount = 0;
  const parentSessionId: string | null = null;
  let relationshipType: string | null = null;
  let sawSidechain = false;
  let entrypoint: string | undefined;
  let promptSource: string | undefined;
  // Latest assistant turn's context-window occupancy (in file order).
  let contextUsedTokens: number | undefined;
  let latestModel: string | undefined;
  let latestCwd: string | null = null;
  let rawOrdinal = 0;
  let malformedRecords = 0;
  const contextObservations: SessionContextObservation[] = [];

  const lines = jsonlContent.split('\n');

  for (const rawLine of lines) {
    const trimmed = rawLine.trim();
    if (!trimmed) continue;

    let line: ClaudeCodeLine;
    try {
      line = JSON.parse(trimmed) as ClaudeCodeLine;
    } catch {
      malformedRecords++;
      rawOrdinal++;
      continue;
    }
    const recordOrdinal = rawOrdinal++;

    const lineType = line.type;
    if (typeof line.cwd === 'string' && line.cwd.trim()) latestCwd = line.cwd;
    const projectIdentity = projectIdentityFromCwd(latestCwd);

    if (lineType === 'system' && line.subtype === 'compact_boundary') {
      contextObservations.push({
        ordinal: recordOrdinal,
        kind: 'compaction',
        source: 'claude_jsonl',
        timestamp: line.timestamp ?? null,
        projectIdentity: projectIdentity ?? undefined,
      });
    }

    if (!lineType) {
      continue;
    }

    if (line.isSidechain) {
      sawSidechain = true;
    }

    // Invocation-mode signal rides every line; capture the first seen.
    if (entrypoint === undefined && typeof line.entrypoint === 'string') entrypoint = line.entrypoint;
    if (promptSource === undefined && typeof line.promptSource === 'string') promptSource = line.promptSource;

    // Only process user and assistant message lines
    if (lineType !== 'user' && lineType !== 'assistant') continue;

    const msg = line.message;
    if (!msg || !msg.role) continue;

    // Extract content blocks
    const rawContent = msg.content;
    if (rawContent == null) continue;

    // Normalize content to array of blocks
    let blocks: ContentBlock[];
    if (typeof rawContent === 'string') {
      blocks = [{ type: 'text', text: rawContent }];
    } else if (Array.isArray(rawContent)) {
      blocks = rawContent as ContentBlock[];
    } else {
      continue;
    }

    // Filter to known block types and normalize
    const normalizedBlocks: ContentBlock[] = [];
    let hasThinking = false;
    let hasToolUse = false;

    for (const block of blocks) {
      if (!block || typeof block !== 'object' || !block.type) continue;

      switch (block.type) {
        case 'text':
          normalizedBlocks.push({ type: 'text', text: block.text ?? '' });
          break;
        case 'thinking':
          normalizedBlocks.push({ type: 'thinking', text: block.thinking ?? '' });
          hasThinking = true;
          break;
        case 'tool_use':
          normalizedBlocks.push({
            type: 'tool_use',
            id: block.id,
            name: block.name,
            input: block.input,
          });
          hasToolUse = true;

          // Extract tool call record
          if (block.name) {
            if (block.name === 'Skill') {
              const skillName = extractExplicitSkillName(
                block.input != null ? JSON.stringify(block.input) : null,
              );
              if (skillName) {
                contextObservations.push({
                  ordinal: recordOrdinal,
                  kind: 'consultation',
                  source: 'claude_skill_tool',
                  timestamp: line.timestamp ?? null,
                  skillName,
                  projectIdentity: projectIdentity ?? undefined,
                  metadata: { toolUseId: block.id ?? null },
                });
              }
            }
            toolCalls.push({
              session_id: sessionId,
              tool_name: block.name,
              category: categorizeToolName(block.name),
              tool_use_id: block.id ?? null,
              input_json: block.input != null ? JSON.stringify(block.input) : null,
              subagent_session_id: extractSubagentSessionId(block.input),
              message_ordinal: messages.length, // current message index
            });
          }
          break;
        case 'tool_result':
          normalizedBlocks.push({
            type: 'tool_result',
            tool_use_id: block.tool_use_id,
            content: block.content,
            is_error: block.is_error,
          });
          break;
        default:
          // Keep unknown block types as-is for forward compatibility
          normalizedBlocks.push(block);
          break;
      }
    }

    const contentJson = JSON.stringify(normalizedBlocks);
    const timestamp = line.timestamp ?? null;

    // Track timestamps for session metadata
    if (timestamp) {
      if (!startedAt || timestamp < startedAt) startedAt = timestamp;
      if (!endedAt || timestamp > endedAt) endedAt = timestamp;
    }

    // Track first user message
    if (msg.role === 'user') {
      userMessageCount++;
      if (firstUserMessage === null) {
        firstUserMessage = previewTextFromBlocks(normalizedBlocks, line.isMeta === true);
      }
    }

    // Track the latest assistant turn's context-window occupancy. The prompt
    // size (input + cache_read + cache_creation) of the most recent request is
    // how full the window is right now; output_tokens is generation, not window.
    if (msg.role === 'assistant' && msg.usage) {
      const u = msg.usage;
      contextUsedTokens =
        (u.input_tokens ?? 0) +
        (u.cache_read_input_tokens ?? 0) +
        (u.cache_creation_input_tokens ?? 0);
      if (typeof msg.model === 'string') latestModel = msg.model;
    }

    messages.push({
      session_id: sessionId,
      ordinal: messages.length,
      role: msg.role,
      content: contentJson,
      timestamp,
      has_thinking: hasThinking ? 1 : 0,
      has_tool_use: hasToolUse ? 1 : 0,
      content_length: contentJson.length,
    });
  }

  const project = filePath ? projectFromPath(filePath) : null;
  if (sessionId.startsWith('agent-')) {
    relationshipType = 'subagent';
  } else if (sawSidechain) {
    relationshipType = 'sidechain';
  }

  return {
    messages,
    toolCalls,
    metadata: {
      session_id: sessionId,
      project,
      agent: 'claude',
      first_message: firstUserMessage,
      started_at: startedAt,
      ended_at: endedAt,
      message_count: messages.length,
      user_message_count: userMessageCount,
      parent_session_id: parentSessionId,
      relationship_type: relationshipType,
      mode: claudeInvocationMode(entrypoint, promptSource),
      context_used_tokens: contextUsedTokens,
      model: latestModel,
    },
    skillContext: {
      projectIdentity: projectIdentityFromCwd(latestCwd),
      observations: contextObservations,
      capabilities: {
        orderedConsultations: malformedRecords === 0
          ? { observable: true }
          : { observable: false, reason: 'malformed_source_record' },
        compactionVisibility: malformedRecords === 0
          ? { observable: true }
          : { observable: false, reason: 'malformed_source_record' },
        catalogPresentation: {
          observable: false,
          reason: 'harness_signal_unavailable',
        },
        instructionLoads: {
          observable: false,
          reason: 'instruction_load_signal_absent',
        },
        diagnostics: malformedRecords > 0 ? ['malformed_source_record'] : [],
      },
    },
  };
}

// --- Insert parsed session into database ---

export function insertParsedSession(
  db: Database.Database,
  parsed: ParsedSession,
  filePath: string,
  fileSize: number,
  fileHash: string,
): void {
  const txn = db.transaction(() => {
    const { metadata, messages, toolCalls } = parsed;
    const skillContext = parsed.skillContext;

    // Clear existing data for this session (for re-parse)
    db.prepare(`
      DELETE FROM session_catalog_observation_entries
      WHERE observation_id IN (
        SELECT id FROM session_context_observations WHERE session_id = ?
      )
    `).run(metadata.session_id);
    db.prepare('DELETE FROM session_context_observations WHERE session_id = ?').run(metadata.session_id);
    db.prepare('DELETE FROM tool_calls WHERE session_id = ?').run(metadata.session_id);
    db.prepare('DELETE FROM messages WHERE session_id = ?').run(metadata.session_id);
    db.prepare('DELETE FROM browsing_sessions WHERE id = ?').run(metadata.session_id);

    // Backfill context-window occupancy so cards populate on import/boot, not
    // only after the next live turn. Mirrors the live adapters' resolution: the
    // resolver branches per agent (Claude 1M default, Codex reported-else-config
    // default), and returns null when the agent/usage yields no window.
    const usedTokens = metadata.context_used_tokens ?? null;
    const contextWindow = usedTokens != null
      ? resolveContextWindow({
          agent: metadata.agent,
          model: metadata.model,
          reportedWindow: metadata.context_window_reported,
          observedTokens: usedTokens,
          codexDefaultWindow: config.contextWindow.codexDefault,
        })
      : null;

    // Insert browsing session
    db.prepare(`
      INSERT INTO browsing_sessions (
        id, project, agent, first_message, started_at, ended_at, message_count,
        user_message_count, parent_session_id, relationship_type, file_path,
        file_size, file_hash, context_used_tokens, context_window_tokens,
        project_identity, skill_context_capabilities_json
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      metadata.session_id,
      metadata.project,
      metadata.agent,
      metadata.first_message,
      metadata.started_at,
      metadata.ended_at,
      metadata.message_count,
      metadata.user_message_count,
      metadata.parent_session_id,
      metadata.relationship_type,
      filePath,
      fileSize,
      fileHash,
      usedTokens,
      contextWindow,
      skillContext?.projectIdentity ?? null,
      skillContext ? JSON.stringify(skillContext.capabilities) : null,
    );

    // Insert messages and collect their IDs for tool call linking
    const insertMsg = db.prepare(`
      INSERT INTO messages (session_id, ordinal, role, content, timestamp, has_thinking, has_tool_use, content_length)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `);

    const messageIds: number[] = [];
    for (const msg of messages) {
      const result = insertMsg.run(
        msg.session_id,
        msg.ordinal,
        msg.role,
        msg.content,
        msg.timestamp,
        msg.has_thinking,
        msg.has_tool_use,
        msg.content_length,
      );
      messageIds.push(Number(result.lastInsertRowid));
    }

    // Insert tool calls linked to their messages
    const insertTc = db.prepare(`
      INSERT INTO tool_calls (message_id, session_id, tool_name, category, tool_use_id, input_json, subagent_session_id)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    `);

    const subagentSessionIds = new Set<string>();
    for (const tc of toolCalls) {
      const messageId = messageIds[tc.message_ordinal];
      if (messageId != null) {
        insertTc.run(
          messageId,
          tc.session_id,
          tc.tool_name,
          tc.category,
          tc.tool_use_id,
          tc.input_json,
          tc.subagent_session_id,
        );
        if (tc.subagent_session_id) {
          subagentSessionIds.add(tc.subagent_session_id);
        }
      }
    }

    const insertObservation = db.prepare(`
      INSERT INTO session_context_observations (
        session_id, ordinal, kind, source, observed_at, skill_name,
        command_fingerprint, project_identity, reason, metadata_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `);
    const insertCatalogEntry = db.prepare(`
      INSERT INTO session_catalog_observation_entries (
        observation_id, ordinal, skill_name, description,
        description_fingerprint, source_location, scope
      ) VALUES (?, ?, ?, ?, ?, ?, ?)
    `);
    for (const observation of skillContext?.observations ?? []) {
      const result = insertObservation.run(
        metadata.session_id,
        observation.ordinal,
        observation.kind,
        observation.source,
        observation.timestamp,
        observation.skillName ?? null,
        observation.commandFingerprint ?? null,
        observation.projectIdentity ?? null,
        observation.reason ?? null,
        JSON.stringify(observation.metadata ?? {}),
      );
      const observationId = Number(result.lastInsertRowid);
      for (const [entryOrdinal, entry] of (observation.catalogEntries ?? []).entries()) {
        insertCatalogEntry.run(
          observationId,
          entryOrdinal,
          entry.name,
          entry.description,
          entry.descriptionFingerprint,
          entry.sourceLocation,
          entry.scope,
        );
      }
    }

    linkParsedSessionRelationships(db, metadata.session_id, [...subagentSessionIds]);
  });

  txn();
}
