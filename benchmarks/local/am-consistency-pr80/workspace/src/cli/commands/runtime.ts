import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { resolveDbPath } from '../../db-path.js';
import { parseIntegerOption, parseOptionSet, rejectExtraPositionals } from '../args.js';
import { registerCommand } from '../commands.js';
import { CliError, invalidUsage } from '../errors.js';
import { effectiveBaseUrl, fetchJson } from '../http.js';
import { writeHuman, writeJson, writeStdout } from '../output.js';
import { runPortlessServe } from '../portless.js';
import type { CliContext } from '../output.js';

// Deliberately the same resolver the server uses: a local copy of the default
// would let `amon status` report on a different database than `amon serve` opens.
function effectiveDbPath(): string {
  return resolveDbPath();
}

function setServeEnv(ctx: CliContext, args: string[]): {
  noImport: boolean;
  noPortless: boolean;
  noWatch: boolean;
  port: number;
} {
  const parsed = parseOptionSet(
    args,
    new Set(['--host', '--port']),
    new Set(['--no-browser', '--no-import', '--no-portless', '--no-watch']),
  );
  rejectExtraPositionals(parsed.positionals, 'amon serve [--host <host>] [--port <port>] [--no-portless]');
  const host = parsed.values.get('--host');
  const port = parsed.values.get('--port');
  if (host) process.env.AGENTMONITOR_HOST = host;
  let effectivePort = 3141;
  if (port) {
    const numericPort = parseIntegerOption(port, '--port');
    if (!numericPort || numericPort < 1 || numericPort > 65535) {
      throw invalidUsage(`Invalid --port: ${port}`);
    }
    effectivePort = numericPort;
    process.env.AGENTMONITOR_PORT = String(numericPort);
  } else {
    const envPort = Number.parseInt(process.env.AGENTMONITOR_PORT ?? '', 10);
    if (Number.isFinite(envPort) && envPort >= 1) effectivePort = envPort;
  }
  if (ctx.global.dbPath) process.env.AGENTMONITOR_DB_PATH = ctx.global.dbPath;
  return {
    noImport: parsed.flags.has('--no-import'),
    noPortless: parsed.flags.has('--no-portless'),
    noWatch: parsed.flags.has('--no-watch'),
    port: effectivePort,
  };
}

function openUrl(url: string): void {
  const command = process.platform === 'darwin'
    ? 'open'
    : process.platform === 'win32'
      ? 'cmd'
      : 'xdg-open';
  const args = process.platform === 'win32' ? ['/c', 'start', '', url] : [url];
  const child = spawn(command, args, { detached: true, stdio: 'ignore' });
  child.unref();
}

export function registerRuntimeCommands(): void {
  registerCommand({
    name: 'serve',
    group: 'Runtime Commands',
    summary: 'Start AgentMonitor at https://agentmonitor.localhost',
    usage: 'serve [--host <host>] [--port <port>] [--no-import] [--no-watch] [--no-portless]',
    examples: ['serve', 'serve --no-portless', 'serve --port 3999 --no-import --no-watch'],
    async handler(ctx, args) {
      const options = setServeEnv(ctx, args);
      if (!options.noPortless) {
        await runPortlessServe(options.port, args);
        return;
      }
      const { installRuntimeSignalHandlers, startAgentMonitorRuntime } = await import('../../runtime.js');
      const { RuntimeOwnershipError } = await import('../../runtime-ownership.js');
      const runtime = startAgentMonitorRuntime(options);
      installRuntimeSignalHandlers(runtime);
      try {
        await runtime;
      } catch (error) {
        if (error instanceof RuntimeOwnershipError) throw new CliError(error.message);
        throw error;
      }
      await new Promise<void>(() => undefined);
    },
  });

  registerCommand({
    name: 'health',
    group: 'Runtime Commands',
    summary: 'Check the HTTP service health endpoint',
    usage: 'health [--url <url>] [--json]',
    examples: ['health', 'health --url http://127.0.0.1:3141 --json'],
    async handler(ctx, args) {
      rejectExtraPositionals(args, 'amon health [--url <url>]');
      const baseUrl = effectiveBaseUrl(ctx.global.url);
      const payload = await fetchJson(`${baseUrl}/api/health`);
      if (ctx.global.json) {
        writeJson(ctx, payload);
      } else {
        writeHuman(ctx, `AgentMonitor is healthy at ${baseUrl}`);
      }
    },
  });

  registerCommand({
    name: 'status',
    group: 'Runtime Commands',
    summary: 'Show local DB and server status',
    usage: 'status [--url <url>] [--json]',
    examples: ['status', 'status --json'],
    async handler(ctx, args) {
      rejectExtraPositionals(args, 'amon status [--url <url>]');
      const baseUrl = effectiveBaseUrl(ctx.global.url);
      // --db-path is already exported into the env by the dispatcher, so the
      // resolver sees it; a relative one stays cwd-relative, the default is absolute.
      const dbPath = path.resolve(effectiveDbPath());
      let server: unknown = null;
      let serverReachable: boolean;
      try {
        server = await fetchJson(`${baseUrl}/api/health`, 1000);
        serverReachable = true;
      } catch {
        serverReachable = false;
      }
      const payload = {
        url: baseUrl,
        db_path: dbPath,
        db_exists: fs.existsSync(dbPath),
        server_reachable: serverReachable,
        server,
      };
      if (ctx.global.json) {
        writeJson(ctx, payload);
        return;
      }
      writeStdout(ctx, [
        `URL: ${payload.url}`,
        `DB: ${payload.db_path} (${payload.db_exists ? 'exists' : 'missing'})`,
        `Server: ${payload.server_reachable ? 'reachable' : 'unavailable'}`,
      ].join('\n'));
    },
  });

  registerCommand({
    name: 'open',
    group: 'Runtime Commands',
    summary: 'Open the Svelte app in the default browser',
    usage: 'open [--url <url>]',
    examples: ['open'],
    handler(ctx, args) {
      rejectExtraPositionals(args, 'amon open [--url <url>]');
      const appUrl = `${effectiveBaseUrl(ctx.global.url)}/app/`;
      openUrl(appUrl);
      writeHuman(ctx, `Opened ${appUrl}`);
    },
  });
}
