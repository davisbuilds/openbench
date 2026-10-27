import { CliError, unavailable } from './errors.js';

function defaultBaseUrl(): string {
  const host = process.env.AGENTMONITOR_HOST || '127.0.0.1';
  const port = process.env.AGENTMONITOR_PORT || '3141';
  return `http://${host}:${port}`;
}

export function effectiveBaseUrl(url?: string): string {
  return (url || process.env.AGENTMONITOR_URL || defaultBaseUrl()).replace(/\/+$/, '');
}

export async function fetchJson(url: string, timeoutMs = 2500): Promise<unknown> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok) throw unavailable(`${url} returned ${res.status}`);
    return await res.json() as unknown;
  } catch (error) {
    if (error instanceof CliError) throw error;
    throw unavailable(`Cannot reach ${url}`);
  } finally {
    clearTimeout(timeout);
  }
}
