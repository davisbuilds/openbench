import { Router, type Request, type Response } from 'express';
import { insertEvent, getEvents } from '../db/queries.js';
import { broadcaster } from '../sse/emitter.js';
import { normalizeIngestEvent } from '../contracts/event-contract.js';
import { coerceJsonLikeBody } from './json-body.js';
import { safelyMaintainTraceSummaryForEvent } from '../trace-quality/service.js';

export const eventsRouter = Router();

// POST /api/events - Ingest single event
eventsRouter.post('/', (req: Request, res: Response) => {
  const parsed = normalizeIngestEvent(coerceJsonLikeBody(req.body));
  if (!parsed.ok) {
    res.status(400).json({
      error: 'Invalid event payload',
      details: parsed.errors,
    });
    return;
  }

  const event = insertEvent(parsed.event);

  if (!event) {
    res.status(200).json({ received: 0, ids: [], duplicates: 1 });
    return;
  }

  broadcaster.broadcast('event', event as unknown as Record<string, unknown>);
  safelyMaintainTraceSummaryForEvent(event.id, 'api event ingest');
  res.status(201).json({ received: 1, ids: [event.id], duplicates: 0 });
});

// POST /api/events/batch - Ingest multiple events
eventsRouter.post('/batch', (req: Request, res: Response) => {
  const body = coerceJsonLikeBody(req.body);
  const { events } = (body && typeof body === 'object') ? body as { events?: unknown } : {};

  if (!Array.isArray(events)) {
    res.status(400).json({ error: 'Expected { events: [...] }' });
    return;
  }

  const ids: number[] = [];
  let duplicates = 0;
  const rejected: Array<{ index: number; errors: string[] }> = [];

  for (let i = 0; i < events.length; i += 1) {
    const parsed = normalizeIngestEvent(events[i]);
    if (!parsed.ok) {
      rejected.push({
        index: i,
        errors: parsed.errors.map(err => `${err.field}: ${err.message}`),
      });
      continue;
    }

    const event = insertEvent(parsed.event);
    if (event) {
      ids.push(event.id);
      broadcaster.broadcast('event', event as unknown as Record<string, unknown>);
      safelyMaintainTraceSummaryForEvent(event.id, 'api batch event ingest');
    } else {
      duplicates += 1;
    }
  }

  res.status(201).json({
    received: ids.length,
    ids,
    duplicates,
    rejected,
  });
});

// GET /api/events - Query events
eventsRouter.get('/', (req: Request, res: Response) => {
  const result = getEvents({
    limit: req.query.limit ? parseInt(req.query.limit as string, 10) : undefined,
    offset: req.query.offset ? parseInt(req.query.offset as string, 10) : undefined,
    agentType: req.query.agent_type as string | undefined,
    eventType: req.query.event_type as string | undefined,
    toolName: req.query.tool_name as string | undefined,
    sessionId: req.query.session_id as string | undefined,
    branch: req.query.branch as string | undefined,
    model: req.query.model as string | undefined,
    source: req.query.source as string | undefined,
    since: req.query.since as string | undefined,
    until: req.query.until as string | undefined,
  });

  res.json(result);
});
