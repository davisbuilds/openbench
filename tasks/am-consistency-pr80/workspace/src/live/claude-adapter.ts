import { config } from '../config.js';
import type Database from 'better-sqlite3';
import type { ContentBlock, ParsedMessage, ParsedSession } from '../parser/claude-code.js';
import { resolveContextWindow } from '../pricing/context-windows.js';
import { applyLivePrivacyPolicy, normalizeClaudeBlock, type LivePrivacyPolicy } from './normalize.js';
import {
  clearProjectedSessionStream,
  FULL_PROJECTION_CAPABILITIES,
  insertProjectedItem,
  insertProjectedTurn,
  upsertProjectedSessionSnapshot,
} from './projector.js';

export interface ClaudeLiveSyncResult {
  inserted_turns: number;
  inserted_items: number;
  reset: boolean;
  live_status: string;
  last_item_at: string | null;
}

function parseMessageBlocks(message: ParsedMessage): ContentBlock[] {
  try {
    return JSON.parse(message.content) as ContentBlock[];
  } catch {
    return [{ type: 'text', text: message.content }];
  }
}

function deriveLiveStatus(lastItemAt: string | null): string {
  if (!lastItemAt) return 'available';
  const diffMs = Date.now() - new Date(lastItemAt).getTime();
  if (Number.isNaN(diffMs)) return 'available';
  if (diffMs <= 5 * 60_000) return 'live';
  if (diffMs <= 15 * 60_000) return 'active';
  return 'ended';
}

function resetLiveSession(db: Database.Database, sessionId: string): void {
  clearProjectedSessionStream(db, sessionId);
}

export function syncClaudeLiveSession(
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

  if (parsed.messages.length < existingTurnCount) {
    resetLiveSession(db, sessionId);
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
    const sourceTurnId = `claude-message:${message.ordinal}`;
    const blocks = parseMessageBlocks(message);
    const titleBlock = blocks.find(block => block.type === 'text' && typeof block.text === 'string' && block.text.trim());
    const title = titleBlock?.text?.trim().slice(0, 120) || `${message.role} message ${message.ordinal + 1}`;

    const turnId = insertProjectedTurn(db, sessionId, {
      agent_type: parsed.metadata.agent,
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

  const liveStatus = deriveLiveStatus(parsed.metadata.ended_at);
  const usedTokens = parsed.metadata.context_used_tokens ?? null;
  const contextWindow = usedTokens != null
    ? resolveContextWindow({
        agent: parsed.metadata.agent,
        model: parsed.metadata.model,
        observedTokens: usedTokens,
      })
    : null;
  upsertProjectedSessionSnapshot(db, {
    id: sessionId,
    agent: parsed.metadata.agent,
    project: parsed.metadata.project,
    first_message: parsed.metadata.first_message,
    started_at: parsed.metadata.started_at,
    ended_at: parsed.metadata.ended_at,
    message_count: parsed.metadata.message_count,
    user_message_count: parsed.metadata.user_message_count,
    live_status: liveStatus,
    last_item_at: parsed.metadata.ended_at,
    context_used_tokens: usedTokens,
    context_window_tokens: contextWindow,
  }, {
    integration_mode: 'claude-jsonl',
    fidelity: 'full',
    capabilities: FULL_PROJECTION_CAPABILITIES,
  });

  return {
    inserted_turns: insertedTurns,
    inserted_items: insertedItems,
    reset,
    live_status: liveStatus,
    last_item_at: parsed.metadata.ended_at,
  };
}
