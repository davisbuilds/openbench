import { config } from '../config.js';
import { upsertProviderQuotaSnapshot } from '../db/queries.js';
import { fetchCodexQuotaSnapshot } from './codex.js';

let codexQuotaTimer: ReturnType<typeof setInterval> | null = null;
let codexQuotaRefreshInFlight: Promise<void> | null = null;

function refreshCodexQuotaSnapshot(): Promise<void> {
  if (codexQuotaRefreshInFlight) return codexQuotaRefreshInFlight;
  codexQuotaRefreshInFlight = (async () => {
    const snapshot = await fetchCodexQuotaSnapshot();
    upsertProviderQuotaSnapshot(snapshot);
  })().finally(() => {
    codexQuotaRefreshInFlight = null;
  });
  return codexQuotaRefreshInFlight;
}

export function startProviderQuotaPolling(): void {
  if (codexQuotaTimer) return;
  void refreshCodexQuotaSnapshot();
  codexQuotaTimer = setInterval(() => {
    void refreshCodexQuotaSnapshot();
  }, config.quotas.codexPollIntervalMs);
}

export async function stopProviderQuotaPolling(): Promise<void> {
  if (codexQuotaTimer) {
    clearInterval(codexQuotaTimer);
    codexQuotaTimer = null;
  }
  await codexQuotaRefreshInFlight;
}
