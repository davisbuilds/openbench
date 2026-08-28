import { spawn, type ChildProcessByStdio } from 'node:child_process';
import type { Readable, Writable } from 'node:stream';
import type { ProviderQuotaSnapshotInput } from '../db/queries.js';

type CodexAppServerProcess = ChildProcessByStdio<Writable, Readable, Readable>;

/** Test seam: lets tests inject a fake JSON-RPC peer without shelling out. */
export interface FetchCodexQuotaOptions {
  spawn?: () => CodexAppServerProcess;
}

interface JsonRpcResponse {
  id?: number;
  result?: unknown;
  error?: { message?: string };
}

function parseJsonLines(buffer: string, onMessage: (message: JsonRpcResponse) => void): string {
  let remainder = buffer;
  while (true) {
    const newlineIndex = remainder.indexOf('\n');
    if (newlineIndex === -1) break;
    const line = remainder.slice(0, newlineIndex).trim();
    remainder = remainder.slice(newlineIndex + 1);
    if (!line) continue;
    try {
      onMessage(JSON.parse(line) as JsonRpcResponse);
    } catch {
      // Ignore non-JSON log lines from the child process.
    }
  }
  return remainder;
}

function buildQuotaWindow(window: unknown): ProviderQuotaSnapshotInput['primary'] {
  if (!window || typeof window !== 'object') return null;
  const data = window as {
    usedPercent?: number | null;
    resetsAt?: number | null;
    windowDurationMins?: number | null;
  };
  return {
    used_percent: typeof data.usedPercent === 'number' ? data.usedPercent : null,
    resets_at: typeof data.resetsAt === 'number' ? data.resetsAt : null,
    window_minutes: typeof data.windowDurationMins === 'number' ? data.windowDurationMins : null,
  };
}

export async function fetchCodexQuotaSnapshot(
  timeoutMs: number = 10_000,
  options: FetchCodexQuotaOptions = {},
): Promise<ProviderQuotaSnapshotInput> {
  return await new Promise((resolve) => {
    let settled = false;
    let stdoutBuffer = '';
    let stderrBuffer = '';
    const responses = new Map<number, JsonRpcResponse>();
    const child = options.spawn
      ? options.spawn()
      : (spawn('codex', ['app-server', '--listen', 'stdio://'], {
          stdio: ['pipe', 'pipe', 'pipe'],
        }) as CodexAppServerProcess);

    const finish = (snapshot: ProviderQuotaSnapshotInput): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.kill();
      resolve(snapshot);
    };

    const successIfReady = (): void => {
      const rateLimitsResponse = responses.get(3)?.result as {
        rateLimits?: unknown;
        rateLimitsByLimitId?: Record<string, unknown> | null;
      } | undefined;

      if (!rateLimitsResponse) return;

      const accountResponse = responses.get(2)?.result as {
        account?: { email?: string | null; planType?: string | null } | null;
      } | undefined;

      const rateLimits = (rateLimitsResponse.rateLimitsByLimitId?.['codex'] ?? rateLimitsResponse.rateLimits) as {
        credits?: { hasCredits?: boolean; unlimited?: boolean; balance?: string | null } | null;
        limitId?: string | null;
        limitName?: string | null;
        planType?: string | null;
        primary?: unknown;
        secondary?: unknown;
        rateLimitReachedType?: string | null;
      } | undefined;

      if (!rateLimits) return;

      finish({
        provider: 'codex',
        agent_type: 'codex',
        status: 'available',
        source: 'codex-app-server',
        account_label: accountResponse?.account?.email ?? null,
        plan_type: rateLimits.planType ?? accountResponse?.account?.planType ?? null,
        limit_id: rateLimits.limitId ?? null,
        limit_name: rateLimits.limitName ?? null,
        error_message: rateLimits.rateLimitReachedType ?? null,
        primary: buildQuotaWindow(rateLimits.primary),
        secondary: buildQuotaWindow(rateLimits.secondary),
        credits: rateLimits.credits
          ? {
              has_credits: Boolean(rateLimits.credits.hasCredits),
              unlimited: Boolean(rateLimits.credits.unlimited),
              balance: rateLimits.credits.balance ?? null,
            }
          : null,
        raw_payload: {
          account: accountResponse ?? null,
          rate_limits: rateLimitsResponse,
        },
      });
    };

    const timer = setTimeout(() => {
      finish({
        provider: 'codex',
        agent_type: 'codex',
        status: 'error',
        source: 'codex-app-server',
        error_message: `Timed out waiting for Codex quota response after ${timeoutMs}ms`,
      });
    }, timeoutMs);

    child.on('error', (error) => {
      finish({
        provider: 'codex',
        agent_type: 'codex',
        status: 'error',
        source: 'codex-app-server',
        error_message: error.message,
      });
    });

    child.stdout.on('data', (chunk: Buffer) => {
      stdoutBuffer += chunk.toString('utf8');
      stdoutBuffer = parseJsonLines(stdoutBuffer, (message) => {
        if (typeof message.id === 'number') {
          responses.set(message.id, message);
          successIfReady();
        }
      });
    });

    child.stderr.on('data', (chunk: Buffer) => {
      stderrBuffer += chunk.toString('utf8');
    });

    child.on('exit', () => {
      if (settled) return;
      // `??` only catches null/undefined, not empty strings — and codex-cli ≥0.133.0
      // exits silently (empty stderr) when stdin EOFs before responses are written.
      // Fall through any empty fallbacks so the user gets an actionable message.
      const rpcError = responses.get(3)?.error?.message?.trim();
      const stderr = stderrBuffer.trim();
      const errorMessage =
        (rpcError && rpcError.length > 0 ? rpcError : null) ??
        (stderr.length > 0 ? stderr : null) ??
        'Codex app-server exited before returning quota data (check `codex --version` and that you are signed in).';
      finish({
        provider: 'codex',
        agent_type: 'codex',
        status: 'error',
        source: 'codex-app-server',
        error_message: errorMessage,
      });
    });

    child.stdin.write('{"id":1,"method":"initialize","params":{"clientInfo":{"name":"agentmonitor","version":"1.0.0"},"capabilities":{"experimentalApi":false}}}\n');
    child.stdin.write('{"method":"initialized"}\n');
    child.stdin.write('{"id":2,"method":"account/read","params":{}}\n');
    child.stdin.write('{"id":3,"method":"account/rateLimits/read","params":null}\n');
    // NOTE: do not call `child.stdin.end()` here. codex-cli ≥0.133.0 detects the
    // stdin EOF before flushing responses and exits silently with empty output,
    // which made the Monitor card show "unavailable" with no diagnostic. Cleanup
    // happens via the `child.kill()` call inside `finish()`.
  });
}
