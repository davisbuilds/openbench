import { once } from 'node:events';
import type { Server } from 'node:http';
import { config } from './config.js';
import { closeDb } from './db/connection.js';
import { initSchema } from './db/schema.js';
import { updateIdleSessions } from './db/queries.js';
import { startStatsBroadcast, stopStatsBroadcast } from './api/stream.js';
import { liveBroadcaster } from './api/v2/live-stream.js';
import { broadcaster } from './sse/emitter.js';
import { createApp } from './app.js';
import { runImport } from './import/index.js';
import { startProviderQuotaPolling, stopProviderQuotaPolling } from './provider-quotas/service.js';
import { acquireRuntimeOwnership } from './runtime-ownership.js';
import { startWatcher, stopWatcher } from './watcher/service.js';
import { ensureSessionTraceSummaryBackfill } from './trace-quality/summary.js';

export interface RuntimeOptions {
  noWatch?: boolean;
  noImport?: boolean;
}

export interface RuntimeHandle {
  url: string;
  close: () => Promise<void>;
}

export async function startAgentMonitorRuntime(options: RuntimeOptions = {}): Promise<RuntimeHandle> {
  const ownership = acquireRuntimeOwnership(config.dbPath);
  let server: Server | undefined;
  let sessionChecker: ReturnType<typeof setInterval> | undefined;
  let autoImportTimer: ReturnType<typeof setInterval> | undefined;
  let autoImportDelay: ReturnType<typeof setTimeout> | undefined;
  let closePromise: Promise<void> | undefined;

  function autoImportAll() {
    try {
      const result = runImport({ source: 'all' });
      if (result.totalEventsImported > 0 || result.totalEventsRefreshed > 0) {
        console.log(`Auto-import: imported ${result.totalEventsImported} and refreshed ${result.totalEventsRefreshed} events from ${result.totalFiles - result.skippedFiles} file(s)`);
        if (broadcaster.clientCount > 0) {
          broadcaster.broadcast('session_update', {
            type: 'auto_import',
            imported: result.totalEventsImported,
            refreshed: result.totalEventsRefreshed,
          });
        }
      }
    } catch (err) {
      console.error('Auto-import error:', err);
    }
  }

  async function close(): Promise<void> {
    if (closePromise) return closePromise;
    closePromise = (async () => {
      const errors: unknown[] = [];
      const attempt = async (operation: () => void | Promise<void>) => {
        try {
          await operation();
        } catch (error) {
          errors.push(error);
        }
      };

      if (sessionChecker) clearInterval(sessionChecker);
      if (autoImportTimer) clearInterval(autoImportTimer);
      if (autoImportDelay) clearTimeout(autoImportDelay);

      // Stop accepting connections before ending SSE responses. EventSource
      // clients reconnect automatically when a stream closes; leaving the
      // listener open during the awaited service cleanup can admit a new stream
      // that keeps server.close() pending indefinitely.
      const serverClosePromise = server?.listening
        ? new Promise<void>((resolve, reject) => {
            server?.close(error => error ? reject(error) : resolve());
          })
        : undefined;
      // Attach a handler immediately in case close fails before the later await.
      void serverClosePromise?.catch(() => undefined);

      stopStatsBroadcast();
      broadcaster.closeAllClients();
      liveBroadcaster.closeAllClients();
      server?.closeIdleConnections();

      await attempt(stopProviderQuotaPolling);
      await attempt(stopWatcher);
      if (serverClosePromise) await attempt(() => serverClosePromise);
      await attempt(() => closeDb());
      await attempt(() => ownership.release());

      if (errors.length > 0) {
        throw new AggregateError(errors, 'AgentMonitor runtime cleanup failed');
      }
    })();
    return closePromise;
  }

  try {
    initSchema();
    ensureSessionTraceSummaryBackfill();

    const app = createApp();
    server = app.listen(config.port, config.host);
    await once(server, 'listening');

    const publicUrl = process.env.PORTLESS_URL?.replace(/\/+$/, '');
    console.log(`AgentMonitor listening on http://${config.host}:${config.port}`);
    console.log(`Dashboard: ${publicUrl ? `${publicUrl}/app/` : `http://localhost:${config.port}/app/`}`);

    startStatsBroadcast();
    if (!options.noWatch) startWatcher();
    startProviderQuotaPolling();

    sessionChecker = setInterval(() => {
      const idled = updateIdleSessions(config.sessionTimeoutMinutes);
      if (idled > 0 && broadcaster.clientCount > 0) {
        broadcaster.broadcast('session_update', { type: 'idle_check', idled });
      }
    }, 60_000);

    if (!options.noImport && config.autoImportIntervalMinutes > 0) {
      const intervalMs = config.autoImportIntervalMinutes * 60_000;
      autoImportDelay = setTimeout(autoImportAll, 5_000);
      autoImportTimer = setInterval(autoImportAll, intervalMs);
      console.log(`Auto-import: every ${config.autoImportIntervalMinutes}m`);
    }

    return {
      url: `http://${config.host}:${config.port}`,
      close,
    };
  } catch (error) {
    try {
      await close();
    } catch (cleanupError) {
      throw new AggregateError(
        [error, cleanupError],
        'AgentMonitor startup and cleanup failed',
        { cause: cleanupError },
      );
    }
    throw error;
  }
}

export function installRuntimeSignalHandlers(runtime: RuntimeHandle | Promise<RuntimeHandle>): void {
  const runtimeReady = Promise.resolve(runtime);
  let closing = false;
  async function shutdown() {
    if (closing) return;
    closing = true;
    console.log('\nShutting down AgentMonitor...');
    try {
      const handle = await runtimeReady;
      await handle.close();
      process.exit(0);
    } catch (error) {
      console.error('AgentMonitor shutdown failed:', error);
      process.exit(1);
    }
  }

  process.on('SIGINT', () => {
    void shutdown();
  });
  process.on('SIGTERM', () => {
    void shutdown();
  });
}
