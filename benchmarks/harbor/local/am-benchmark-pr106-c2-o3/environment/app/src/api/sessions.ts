import { Router, type Request, type Response } from 'express';
import { getSessions, getSessionWithEvents } from '../db/queries.js';

export const sessionsRouter = Router();

// GET /api/sessions - List sessions
sessionsRouter.get('/', (req: Request, res: Response) => {
  const sessions = getSessions({
    status: req.query.status as string | undefined,
    excludeStatus: req.query.exclude_status as string | undefined,
    agentType: req.query.agent_type as string | undefined,
    since: req.query.since as string | undefined,
    limit: req.query.limit ? parseInt(req.query.limit as string, 10) : undefined,
  });

  res.json({ sessions, total: sessions.length });
});

// GET /api/sessions/:id - Session detail with events
sessionsRouter.get('/:id', (req: Request, res: Response) => {
  const limit = req.query.event_limit
    ? parseInt(req.query.event_limit as string, 10)
    : req.query.limit ? parseInt(req.query.limit as string, 10) : 10;
  const result = getSessionWithEvents(req.params['id'] as string, limit);

  if (!result.session) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }

  res.json(result);
});
