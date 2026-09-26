/**
 * Trace-quality ingest hooks.
 *
 * The reframe (Phase 3) stopped persisting the trace/observation warehouse:
 * ingestion now only maintains the lean, content-free `session_trace_summary`
 * rollup. Per-session trace detail is projected on-demand
 * (`src/trace-quality/on-demand.ts`) and is never stored. The old persisted
 * tables are dropped by `scripts/reclaim-trace-quality.ts`.
 */
import { bumpSessionTraceSummaryForEvent, maintainSessionTraceSummary } from './summary.js';

/**
 * Maintain the session summary after an event is ingested. O(1) incremental for
 * event-sourced sessions; failures are swallowed so ingestion never breaks on a
 * summary update.
 */
export function safelyMaintainTraceSummaryForEvent(eventId: number, context: string): void {
  try {
    bumpSessionTraceSummaryForEvent(eventId);
  } catch (err) {
    console.error(`[trace-quality] Failed to update summary for event ${eventId} after ${context}:`, err);
  }
}

/** Re-derive and persist the session summary after a session file sync. */
export function safelyMaintainTraceSummaryForSession(sessionId: string, context: string): void {
  try {
    maintainSessionTraceSummary(sessionId);
  } catch (err) {
    console.error(`[trace-quality] Failed to update summary for session ${sessionId} after ${context}:`, err);
  }
}
