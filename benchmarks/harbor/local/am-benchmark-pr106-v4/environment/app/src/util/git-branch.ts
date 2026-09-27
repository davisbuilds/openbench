import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { config } from '../config.js';

// Cache branch lookups for 60s to avoid excessive git calls
const branchCache = new Map<string, { branch: string | null; ts: number }>();
const CACHE_TTL_MS = 60_000;

/**
 * Resolve the current git branch for a project directory.
 * Returns null if the project can't be found or isn't a git repo.
 */
export function resolveGitBranch(project: string): string | null {
  if (!config.projectsDir || !project) return null;

  const cached = branchCache.get(project);
  if (cached && Date.now() - cached.ts < CACHE_TTL_MS) return cached.branch;

  try {
    const dir = path.join(config.projectsDir, project);
    const branch = execFileSync('git', ['rev-parse', '--abbrev-ref', 'HEAD'], {
      cwd: dir,
      timeout: 2000,
      encoding: 'utf-8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim() || null;

    branchCache.set(project, { branch, ts: Date.now() });
    return branch;
  } catch {
    branchCache.set(project, { branch: null, ts: Date.now() });
    return null;
  }
}
