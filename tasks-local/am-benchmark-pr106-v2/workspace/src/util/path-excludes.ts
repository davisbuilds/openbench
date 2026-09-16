import path from 'node:path';

function normalizePattern(pattern: string): string {
  return pattern
    .trim()
    .replace(/[\\/]+/g, path.sep)
    .replace(/[\\/]+$/, '');
}

export function normalizeExcludePatterns(patterns: string[] | undefined): string[] {
  if (!patterns || patterns.length === 0) return [];

  const normalized = patterns
    .map(normalizePattern)
    .filter(pattern => pattern !== '' && pattern !== '.');

  return [...new Set(normalized)];
}

function getRelativePath(rootDir: string, candidatePath: string): string | null {
  const rel = path.relative(path.resolve(rootDir), path.resolve(candidatePath));
  if (rel === '' || rel === '.' || rel === '..' || rel.startsWith(`..${path.sep}`)) {
    return null;
  }

  return path.normalize(rel);
}

function matchesPathPattern(relativePath: string, pattern: string): boolean {
  const parts = relativePath.split(path.sep);
  for (let i = 1; i <= parts.length; i++) {
    const prefix = parts.slice(0, i).join(path.sep);
    if (path.matchesGlob(prefix, pattern)) return true;
  }

  return path.matchesGlob(relativePath, pattern);
}

export function shouldExcludePath(
  rootDir: string,
  candidatePath: string,
  patterns: string[] | undefined,
): boolean {
  const normalizedPatterns = normalizeExcludePatterns(patterns);
  if (normalizedPatterns.length === 0) return false;

  const relativePath = getRelativePath(rootDir, candidatePath);
  if (!relativePath) return false;

  const parts = relativePath.split(path.sep);
  for (const pattern of normalizedPatterns) {
    if (pattern.includes(path.sep)) {
      if (matchesPathPattern(relativePath, pattern)) return true;
      continue;
    }

    if (parts.some(part => path.matchesGlob(part, pattern))) {
      return true;
    }
  }

  return false;
}
