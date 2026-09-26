import fs from 'fs';
import path from 'path';
import { normalizeExcludePatterns, shouldExcludePath } from './path-excludes.js';

interface DiscoveryOptions {
  excludePatterns?: string[];
}

function discoverFilesRecursive(
  rootDir: string,
  predicate: (entry: fs.Dirent, fullPath: string) => boolean,
  options: DiscoveryOptions = {},
): string[] {
  const files: string[] = [];
  const excludePatterns = normalizeExcludePatterns(options.excludePatterns);

  if (!fs.existsSync(rootDir)) return files;

  const stack = [rootDir];
  while (stack.length > 0) {
    const currentDir = stack.pop();
    if (!currentDir) continue;

    for (const entry of fs.readdirSync(currentDir, { withFileTypes: true })) {
      const fullPath = path.join(currentDir, entry.name);
      if (entry.isDirectory()) {
        if (shouldExcludePath(rootDir, fullPath, excludePatterns)) {
          continue;
        }
        stack.push(fullPath);
        continue;
      }

      if (!shouldExcludePath(rootDir, fullPath, excludePatterns) && predicate(entry, fullPath)) {
        files.push(fullPath);
      }
    }
  }

  return files.sort();
}

export function discoverJsonlFilesRecursive(rootDir: string, options: DiscoveryOptions = {}): string[] {
  return discoverFilesRecursive(rootDir, entry => entry.isFile() && entry.name.endsWith('.jsonl'), options);
}

export function discoverDbFilesRecursive(rootDir: string, options: DiscoveryOptions = {}): string[] {
  return discoverFilesRecursive(rootDir, entry => entry.isFile() && entry.name.endsWith('.db'), options);
}
