import express, { type Express, type NextFunction, type Request, type Response } from 'express';
import path from 'path';
import { fileURLToPath } from 'url';
import { apiRouter } from './api/router.js';

export interface CreateAppOptions {
  serveStatic?: boolean;
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.join(__dirname, '..', 'public');

export function createApp(options: CreateAppOptions = {}): Express {
  const app = express();
  const jsonLikeContentTypes = ['application/json', 'application/*+json', 'text/plain'];

  // Parse ingest routes as raw text first so handlers can recover from
  // double-encoded payloads sent by flaky clients.
  app.use('/api/events', express.text({ limit: '1mb', type: jsonLikeContentTypes }));
  app.use('/api/otel', express.text({ limit: '5mb', type: jsonLikeContentTypes }));
  app.use(express.json({ limit: '1mb', strict: false }));
  app.use('/api', apiRouter);

  // Surface invalid JSON payloads as concise 400s without noisy stack traces.
  app.use((err: unknown, _req: Request, res: Response, next: NextFunction) => {
    if (
      err instanceof SyntaxError
      && 'status' in err
      && (err as { status?: number }).status === 400
      && 'body' in err
    ) {
      res.status(400).json({ error: 'Invalid JSON payload' });
      return;
    }
    next(err);
  });

  // Portless owns the friendly human-facing origin. Preserve the legacy `/`
  // surface on direct loopback access while making the named origin land on
  // the canonical Svelte app.
  app.get('/', (req: Request, res: Response, next: NextFunction) => {
    if (req.hostname.toLowerCase() === 'agentmonitor.localhost') {
      res.redirect(302, '/app/');
      return;
    }
    next();
  });

  if (options.serveStatic !== false) {
    app.use(express.static(publicDir));
  }

  // Serve Svelte SPA at /app (frontend/dist)
  const svelteDir = path.join(__dirname, '..', 'frontend', 'dist');
  app.use('/app', express.static(svelteDir));
  app.get('/app/{*path}', (_req: Request, res: Response) => {
    res.sendFile(path.join(svelteDir, 'index.html'));
  });

  return app;
}
