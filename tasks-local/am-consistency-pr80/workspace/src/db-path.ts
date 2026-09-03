import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The default database path, resolved the same way for every entry point.
 *
 * This lives outside config.ts on purpose. config.ts evaluates
 * `export const config = createConfig()` on import, which snapshots process.env;
 * importing it from the CLI would freeze that snapshot before a command (or a
 * test's `before()` hook) has set AGENTMONITOR_DB_PATH. This module has no
 * side effects, so both the server config and `amon status` can share one
 * resolver without either inheriting the other's import timing.
 */

// src/db-path.ts and dist/db-path.js both sit one level under the package root.
const packageRoot = path.join(path.dirname(fileURLToPath(import.meta.url)), '..');

/**
 * The default database lives with the install, not with the shell.
 *
 * This was `./data/agentmonitor.db` — resolved against the working directory —
 * so `amon serve` from anywhere but the repo root did not fail. It created an
 * empty database there and the auto-importer began filling it, producing a
 * dashboard that looked plausible and was simply about someone else's data.
 * An explicit AGENTMONITOR_DB_PATH is still honored as given.
 */
export function resolveDbPath(env: NodeJS.ProcessEnv = process.env): string {
  const configured = env.AGENTMONITOR_DB_PATH?.trim();
  if (configured) return configured;
  return path.join(packageRoot, 'data', 'agentmonitor.db');
}
