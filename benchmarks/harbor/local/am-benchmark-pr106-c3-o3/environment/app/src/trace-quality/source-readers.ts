import { listTraceQualityEventSourcesForSession } from '../db/queries.js';
import { getTraceQualitySessionSourceRows } from '../db/v2-queries.js';
import type { TraceQualityProjectionInput } from './projection.js';

function firstNonEmpty<T>(values: Array<T | null | undefined>): T | null {
  for (const value of values) {
    if (value != null && value !== '') return value;
  }
  return null;
}

export function readTraceQualityProjectionInputForSession(sessionId: string): TraceQualityProjectionInput {
  const v2Sources = getTraceQualitySessionSourceRows(sessionId);
  const events = listTraceQualityEventSourcesForSession(sessionId);

  const agentType = firstNonEmpty([
    v2Sources.browsingSession?.agent,
    v2Sources.turns[0]?.agent_type,
    events[0]?.agent_type,
  ]);
  const project = firstNonEmpty([
    v2Sources.browsingSession?.project,
    events.find(event => event.project)?.project,
  ]);
  const branch = firstNonEmpty([
    events.find(event => event.branch)?.branch,
  ]);

  return {
    sessionId,
    agentType,
    project,
    branch,
    browsingSession: v2Sources.browsingSession ?? null,
    events,
    turns: v2Sources.turns,
    sessionItems: v2Sources.sessionItems,
    messages: v2Sources.messages,
    toolCalls: v2Sources.toolCalls,
  };
}
