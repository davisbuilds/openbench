import type { Response } from 'express';
import { config } from '../config.js';

interface SSEClient {
  res: Response;
  filters?: {
    agentType?: string;
    eventType?: string;
  };
  heartbeat: ReturnType<typeof setInterval> | null;
  backpressureCount: number;
  cleanup: () => void;
}

class SSEBroadcaster {
  private clients = new Set<SSEClient>();
  private readonly maxClients: number;
  private readonly heartbeatMs: number;
  private readonly maxBackpressureWrites = 3;

  constructor(options: { maxClients: number; heartbeatMs: number }) {
    this.maxClients = options.maxClients;
    this.heartbeatMs = options.heartbeatMs;
  }

  addClient(res: Response, filters?: SSEClient['filters']): boolean {
    if (this.clients.size >= this.maxClients) {
      return false;
    }

    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    });

    const client: SSEClient = {
      res,
      filters,
      heartbeat: null,
      backpressureCount: 0,
      cleanup: () => {
        this.removeClient(client);
      },
    };
    this.clients.add(client);
    res.on('close', client.cleanup);
    res.on('error', client.cleanup);

    this.safeWrite(client, `data: ${JSON.stringify({ type: 'connected', timestamp: new Date().toISOString() })}\n\n`);

    // Heartbeat to keep intermediaries from silently timing out the connection.
    client.heartbeat = setInterval(() => {
      if (!this.safeWrite(client, ': heartbeat\n\n')) {
        this.removeClient(client);
      }
    }, this.heartbeatMs);

    return true;
  }

  broadcast(type: string, payload: Record<string, unknown>): void {
    const message = `data: ${JSON.stringify({ type, payload })}\n\n`;
    for (const client of Array.from(this.clients)) {
      if (client.filters?.agentType && payload.agent_type !== client.filters.agentType) continue;
      if (client.filters?.eventType && payload.event_type !== client.filters.eventType) continue;
      if (!this.safeWrite(client, message)) {
        this.removeClient(client);
      }
    }
  }

  closeAllClients(): void {
    for (const client of Array.from(this.clients)) {
      this.removeClient(client);
    }
  }

  private safeWrite(client: SSEClient, chunk: string): boolean {
    if (!this.clients.has(client)) return false;
    if (client.res.writableEnded || client.res.destroyed) return false;

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

  private removeClient(client: SSEClient): void {
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
        // Ignore errors while tearing down stale sockets.
      }
    }
  }

  get clientCount(): number {
    return this.clients.size;
  }
}

export const broadcaster = new SSEBroadcaster({
  maxClients: config.maxSseClients,
  heartbeatMs: config.sseHeartbeatMs,
});
