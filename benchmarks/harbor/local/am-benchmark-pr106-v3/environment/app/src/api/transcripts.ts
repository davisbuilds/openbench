import { Router, type Request, type Response } from 'express';
import { getSessionTranscript, type TranscriptEvent } from '../db/queries.js';

export const transcriptsRouter = Router();

// --- Transcript entry types ---

interface TranscriptEntry {
  role: 'system' | 'user' | 'assistant' | 'tool';
  type: string;
  tool_name?: string;
  detail?: string;
  status?: string;
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  duration_ms?: number;
  timestamp: string;
}

function extractDetail(event: TranscriptEvent): string | undefined {
  try {
    const meta = JSON.parse(event.metadata || '{}');
    if (meta.message && event.event_type === 'user_prompt') return meta.message;
    if (meta.content_preview) return meta.content_preview;
    if (meta.command) return meta.command;
    if (meta.file_path) return meta.file_path;
    if (meta.pattern) return meta.pattern;
    if (meta.query) return meta.query;
    if (meta.error) return typeof meta.error === 'string' ? meta.error : meta.error?.message;
    if (meta.diff_preview) return meta.diff_preview;
  } catch { /* ignore */ }
  return undefined;
}

function mapRole(eventType: string): 'system' | 'user' | 'assistant' | 'tool' {
  switch (eventType) {
    case 'session_start':
    case 'session_end':
      return 'system';
    case 'user_prompt':
      return 'user';
    case 'tool_use':
      return 'tool';
    case 'error':
      return 'assistant';
    default:
      return 'assistant';
  }
}

function toEntry(event: TranscriptEvent): TranscriptEntry {
  const entry: TranscriptEntry = {
    role: mapRole(event.event_type),
    type: event.event_type,
    timestamp: event.client_timestamp || event.created_at,
  };

  if (event.tool_name) entry.tool_name = event.tool_name;

  const detail = extractDetail(event);
  if (detail) entry.detail = detail;

  if (event.status !== 'success') entry.status = event.status;
  if (event.model) entry.model = event.model;
  if (event.tokens_in > 0) entry.tokens_in = event.tokens_in;
  if (event.tokens_out > 0) entry.tokens_out = event.tokens_out;
  if (event.cost_usd && event.cost_usd > 0) entry.cost_usd = event.cost_usd;
  if (event.duration_ms) entry.duration_ms = event.duration_ms;

  return entry;
}

// GET /api/sessions/:id/transcript
transcriptsRouter.get('/:id/transcript', (req: Request, res: Response) => {
  const sessionId = req.params['id'] as string;
  const events = getSessionTranscript(sessionId);

  if (events.length === 0) {
    res.status(404).json({ error: 'No transcript data for this session' });
    return;
  }

  const entries = events.map(toEntry);

  res.json({
    session_id: sessionId,
    entries,
  });
});
