import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

interface PackageJson {
  name?: string;
  version?: string;
}

let cachedPackage: PackageJson | undefined;

export function findPackageRoot(startUrl: string = import.meta.url): string {
  let current = path.dirname(fileURLToPath(startUrl));
  while (true) {
    const packageJson = path.join(current, 'package.json');
    if (fs.existsSync(packageJson)) {
      try {
        const parsed = JSON.parse(fs.readFileSync(packageJson, 'utf-8')) as PackageJson;
        if (parsed.name === 'agentmonitor') return current;
      } catch {
        // Keep walking; malformed package metadata is handled by callers.
      }
    }
    const parent = path.dirname(current);
    if (parent === current) return process.cwd();
    current = parent;
  }
}

function readPackageJson(): PackageJson {
  if (cachedPackage) return cachedPackage;
  const packagePath = path.join(findPackageRoot(), 'package.json');
  cachedPackage = JSON.parse(fs.readFileSync(packagePath, 'utf-8')) as PackageJson;
  return cachedPackage;
}

export function packageVersion(): string {
  return readPackageJson().version ?? '0.0.0';
}
