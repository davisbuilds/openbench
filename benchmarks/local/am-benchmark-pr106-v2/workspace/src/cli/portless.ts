import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { CliError } from './errors.js';

const PORTLESS_APP_NAME = 'agentmonitor';
const FORWARDED_SIGNALS: NodeJS.Signals[] = ['SIGINT', 'SIGTERM'];

function resolvePortlessCli(): string {
  // The override is an integration-test seam; normal runs always resolve the
  // pinned package dependency next to AgentMonitor.
  const override = process.env.AGENTMONITOR_PORTLESS_CLI?.trim();
  if (override) return override;

  const packageEntry = import.meta.resolve('portless');
  return fileURLToPath(new URL('./cli.js', packageEntry));
}

function currentCliCommand(): string[] {
  const entrypoint = process.argv[1];
  if (!entrypoint) throw new CliError('Cannot locate the AgentMonitor CLI entrypoint');
  return [process.execPath, ...process.execArgv, entrypoint];
}

export async function runPortlessServe(port: number, serveArgs: string[]): Promise<void> {
  const portlessCli = resolvePortlessCli();
  const child = spawn(process.execPath, [
    portlessCli,
    '--name',
    PORTLESS_APP_NAME,
    '--app-port',
    String(port),
    ...currentCliCommand(),
    'serve',
    ...serveArgs,
    '--no-portless',
  ], {
    cwd: process.cwd(),
    env: process.env,
    stdio: 'inherit',
  });

  let forwardedSignal: NodeJS.Signals | null = null;
  const forwardSignal = (signal: NodeJS.Signals) => {
    forwardedSignal ??= signal;
    if (child.exitCode === null && child.signalCode === null) child.kill(signal);
  };
  const signalHandlers = FORWARDED_SIGNALS.map(signal => {
    const handler = () => forwardSignal(signal);
    process.once(signal, handler);
    return { signal, handler };
  });
  const removeSignalHandlers = () => {
    for (const { signal, handler } of signalHandlers) process.removeListener(signal, handler);
  };

  try {
    const result = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve({ code, signal }));
    });

    if (forwardedSignal) {
      // The direct runtime treats a handled Ctrl-C/SIGTERM as a clean shutdown.
      // Match that contract after Portless has removed the route and its child
      // has exited, rather than re-raising the signal in this wrapper process.
      return;
    }
    if (result.signal) {
      removeSignalHandlers();
      process.kill(process.pid, result.signal);
      await new Promise<void>(() => undefined);
      return;
    }
    if (result.code !== 0) {
      throw new CliError(`Portless exited with code ${result.code ?? 1}`);
    }
  } finally {
    removeSignalHandlers();
  }
}
