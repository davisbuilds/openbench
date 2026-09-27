import { Router, type Request, type Response } from 'express';
import { config } from '../../config.js';

interface LiveSSEClient {
  res: Response;
  sessionId?: string;
  heartbeat: ReturnType<typeof setInterval> | null;
  backpressureCount: number;
  cleanup: () => void;
}

interface LiveSSEEvent {
  id: number;
  type: string;
  payload?: Record<string, unknown>;
  timestamp: string;
}

class LiveSSEBroadcaster {
  private clients = new Set<LiveSSEClient>();
  private history: LiveSSEEvent[] = [];
  private nextEventId = 1;
  private readonly historyLimit = 500;
  private readonly maxBackpressureWrites = 3;
  private readonly maxClients: number;
  private readonly heartbeatMs: number;

  constructor(options: { maxClients: number; heartbeatMs: number }) {
    this.maxClients = options.maxClients;
    this.heartbeatMs = options.heartbeatMs;
  }

  addClient(res: Response, options: { sessionId?: string; sinceId?: number } = {}): boolean {
    if (this.clients.size >= this.maxClients) {
      return false;
    }

    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    });

    const client: LiveSSEClient = {
      res,
      sessionId: options.sessionId,
      heartbeat: null,
      backpressureCount: 0,
      cleanup: () => this.removeClient(client),
    };

    this.clients.add(client);
    res.on('close', client.cleanup);
    res.on('error', client.cleanup);

    const replayed = this.replay(client, options.sinceId);
    const connected: LiveSSEEvent = {
      id: this.nextEventId++,
      type: 'connected',
      payload: {
        replayed,
        latest_event_id: this.history[this.history.length - 1]?.id ?? null,
      },
      timestamp: new Date().toISOString(),
    };
    if (!this.safeWrite(client, connected)) {
      this.removeClient(client);
      return false;
    }

    client.heartbeat = setInterval(() => {
      if (!this.safeWriteChunk(client, ': heartbeat\n\n')) {
        this.removeClient(client);
      }
    }, this.heartbeatMs);

    return true;
  }

  broadcast(type: string, payload: Record<string, unknown>): void {
    const event: LiveSSEEvent = {
      id: this.nextEventId++,
      type,
      payload,
      timestamp: new Date().toISOString(),
    };
    this.history.push(event);
    if (this.history.length > this.historyLimit) {
      this.history.splice(0, this.history.length - this.historyLimit);
    }

    for (const client of Array.from(this.clients)) {
      if (client.sessionId && payload.session_id !== client.sessionId) continue;
      if (!this.safeWrite(client, event)) {
        this.removeClient(client);
      }
    }
  }

  resetForTests(): void {
    this.history = [];
    this.nextEventId = 1;
    this.closeAllClients();
  }

  closeAllClients(): void {
    for (const client of Array.from(this.clients)) {
      this.removeClient(client);
    }
  }

  private replay(client: LiveSSEClient, sinceId?: number): number {
    if (sinceId == null) return 0;

    let replayed = 0;
    for (const event of this.history) {
      if (event.id <= sinceId) continue;
      if (client.sessionId && event.payload?.session_id !== client.sessionId) continue;
      if (!this.safeWrite(client, event)) {
        this.removeClient(client);
        break;
      }
      replayed += 1;
    }
    return replayed;
  }

  private safeWrite(client: LiveSSEClient, event: LiveSSEEvent): boolean {
    return this.safeWriteChunk(client, `id: ${event.id}\ndata: ${JSON.stringify(event)}\n\n`);
  }

  private safeWriteChunk(client: LiveSSEClient, chunk: string): boolean {
    if (!this.clients.has(client) || client.res.writableEnded || client.res.destroyed) return false;
    try {
      const ok = client.res.write(chunk);
      if (!ok) {
        client.backpressureCount += 1;
        client.res.once('drain', () => {
          client.backpressureCount = 0;
        });
        if (client.backpressureCount >= this.maxBackpressureWrites) {
          return false;
        }
      } else {
        client.backpressureCount = 0;
      }
      return true;
    } catch {
      return false;
    }
  }

  private removeClient(client: LiveSSEClient): void {
    if (!this.clients.delete(client)) return;
    client.res.off('close', client.cleanup);
    client.res.off('error', client.cleanup);
    if (client.heartbeat) {
      clearInterval(client.heartbeat);
      client.heartbeat = null;
    }
    if (!client.res.writableEnded && !client.res.destroyed) {
      try {
        client.res.end();
      } catch {
        // Ignore teardown errors.
      }
    }
  }

  get clientCount(): number {
    return this.clients.size;
  }
}

export const liveBroadcaster = new LiveSSEBroadcaster({
  maxClients: config.maxSseClients,
  heartbeatMs: config.sseHeartbeatMs,
});
export const liveStreamRouter = Router();

liveStreamRouter.get('/', (req: Request, res: Response) => {
  const sinceRaw = (req.query.since as string | undefined) ?? req.get('last-event-id') ?? undefined;
  const sinceParsed = sinceRaw ? Number.parseInt(sinceRaw, 10) : undefined;
  const accepted = liveBroadcaster.addClient(res, {
    sessionId: req.query.session_id as string | undefined,
    sinceId: Number.isFinite(sinceParsed) ? sinceParsed : undefined,
  });

  if (!accepted) {
    res.status(503).json({
      error: 'SSE client limit reached',
      max_clients: config.maxSseClients,
    });
  }
});
