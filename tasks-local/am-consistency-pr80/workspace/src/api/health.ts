import { Router, type Request, type Response } from 'express';
import fs from 'fs';
import { config } from '../config.js';
import { broadcaster } from '../sse/emitter.js';

export const healthRouter = Router();

const startTime = Date.now();

// GET /api/health - Server status
healthRouter.get('/', (_req: Request, res: Response) => {
  let dbSizeBytes = 0;
  try {
    const stat = fs.statSync(config.dbPath);
    dbSizeBytes = stat.size;
  } catch {
    // DB file may not exist yet
  }

  res.json({
    status: 'ok',
    uptime: Math.floor((Date.now() - startTime) / 1000),
    db_size_bytes: dbSizeBytes,
    sse_clients: broadcaster.clientCount,
  });
});
