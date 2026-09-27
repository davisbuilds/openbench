// Antigravity conversation `.db` → v2 live projection.
//
// Parallel to syncCodexLiveSession (src/live/codex-adapter.ts): projects a
// ParsedSession into session_turns/session_items and stamps
// browsing_sessions.integration_mode/fidelity via the projector. Runs at
// `summary` fidelity because step payload internals are not yet decoded (see
// src/parser/antigravity-sessions.ts).

import type Database from 'better-sqlite3';
import { config } from '../config.js';
import type { ContentBlock, ParsedSession, ParsedMessage } from '../parser/claude-code.js';
import {
  applyLivePrivacyPolicy,
  normalizeClaudeBlock,
  type LivePrivacyPolicy,
} from './normalize.js';
import {
  clearProjectedSessionStream,
  insertProjectedItem,
  insertProjectedTurn,
  upsertProjectedSessionSnapshot,
} from './projector.js';
import type { ClaudeLiveSyncResult } from './claude-adapter.js';

const ANTIGRAVITY_SQLITE_CAPABILITIES = Object.freeze({
  history: 'summary' as const,
  search: 'summary' as const,
  tool_analytics: 'summary' as const,
  live_items: 'summary' as const,
});

function parseMessageBlocks(message: ParsedMessage): ContentBlock[] {
  try {
    return JSON.parse(message.content) as ContentBlock[];
  } catch {
    return [{ type: 'text', text: message.content }];
  }
}

function deriveLiveStatusFromTimestamp(lastItemAt: string | null): string {
  if (!lastItemAt) return 'available';
  const diffMs = Date.now() - new Date(lastItemAt).getTime();
  if (Number.isNaN(diffMs)) return 'available';
  if (diffMs <= 5 * 60_000) return 'live';
  if (diffMs <= 15 * 60_000) return 'active';
  return 'ended';
}

export function syncAntigravityLiveSession(
  db: Database.Database,
  parsed: ParsedSession,
  options: { privacyPolicy?: LivePrivacyPolicy } = {},
): ClaudeLiveSyncResult {
  const sessionId = parsed.metadata.session_id;
  const existingTurnCount = (
    db.prepare('SELECT COUNT(*) as c FROM session_turns WHERE session_id = ?').get(sessionId) as { c: number }
  ).c;

  let reset = false;
  let startOrdinal = existingTurnCount;

  // A shorter step list than we already projected means the DB was rewritten;
  // clear and reproject to stay consistent (mirrors the Codex adapter).
  if (parsed.messages.length < existingTurnCount) {
    clearProjectedSessionStream(db, sessionId);
    reset = true;
    startOrdinal = 0;
  }

  let insertedTurns = 0;
  let insertedItems = 0;
  const privacyPolicy = options.privacyPolicy ?? {
    capturePrompts: config.live.capture.prompts,
    captureReasoning: config.live.capture.reasoning,
    captureToolArguments: config.live.capture.toolArguments,
    diffPayloadMaxBytes: config.live.diffPayloadMaxBytes,
  };

  for (const message of parsed.messages.slice(startOrdinal)) {
    const sourceTurnId = `antigravity-message:${message.ordinal}`;
    const blocks = parseMessageBlocks(message);
    const titleBlock = blocks.find(block => block.type === 'text' && typeof block.text === 'string' && block.text.trim());
    const title = titleBlock?.text?.trim().slice(0, 120) || `${message.role} message ${message.ordinal + 1}`;

    const turnId = insertProjectedTurn(db, sessionId, {
      agent_type: 'antigravity',
      source_turn_id: sourceTurnId,
      status: 'completed',
      title,
      started_at: message.timestamp,
      ended_at: message.timestamp,
    });
    insertedTurns++;

    let itemOrdinal = 0;
    for (const block of blocks) {
      const normalizedItem = normalizeClaudeBlock(
        message.role === 'user' ? 'user' : 'assistant',
        block,
        message.timestamp ?? undefined,
      );
      if (!normalizedItem) continue;
      const normalized = applyLivePrivacyPolicy(normalizedItem, privacyPolicy);

      insertProjectedItem(db, sessionId, turnId, {
        ordinal: itemOrdinal,
        source_item_id: normalized.source_item_id ?? `${sourceTurnId}:item:${itemOrdinal}`,
        kind: normalized.kind,
        status: normalized.status ?? 'success',
        payload: normalized.payload,
        created_at: normalized.created_at ?? message.timestamp,
      });
      insertedItems++;
      itemOrdinal++;
    }
  }

  const liveStatus = deriveLiveStatusFromTimestamp(parsed.metadata.ended_at);
  upsertProjectedSessionSnapshot(db, {
    id: sessionId,
    agent: 'antigravity',
    project: parsed.metadata.project,
    first_message: parsed.metadata.first_message,
    started_at: parsed.metadata.started_at,
    ended_at: parsed.metadata.ended_at,
    message_count: parsed.metadata.message_count,
    user_message_count: parsed.metadata.user_message_count,
    live_status: liveStatus,
    last_item_at: parsed.metadata.ended_at,
  }, {
    integration_mode: 'antigravity-sqlite',
    fidelity: 'summary',
    capabilities: ANTIGRAVITY_SQLITE_CAPABILITIES,
  });

  return {
    inserted_turns: insertedTurns,
    inserted_items: insertedItems,
    reset,
    live_status: liveStatus,
    last_item_at: parsed.metadata.ended_at,
  };
}
