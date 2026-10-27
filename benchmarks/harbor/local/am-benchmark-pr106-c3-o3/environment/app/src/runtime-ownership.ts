import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

interface OwnershipRecord {
  pid: number;
  startedAt: string;
  token: string;
  dbPath: string;
}

export interface RuntimeOwnershipHandle {
  dbPath: string;
  lockPath: string;
  release: () => void;
}

export class RuntimeOwnershipError extends Error {
  readonly dbPath: string;
  readonly ownerPid: number;
  readonly ownerStartedAt: string;

  constructor(record: OwnershipRecord) {
    super(
      `Database is already owned by AgentMonitor runtime PID ${record.pid}: ${record.dbPath}. `
      + 'Stop that runtime before starting another.',
    );
    this.name = 'RuntimeOwnershipError';
    this.dbPath = record.dbPath;
    this.ownerPid = record.pid;
    this.ownerStartedAt = record.startedAt;
  }
}

function canonicalDbPath(dbPath: string): string {
  const absolute = path.resolve(dbPath);
  const parent = path.dirname(absolute);
  fs.mkdirSync(parent, { recursive: true });

  try {
    return fs.realpathSync.native(absolute);
  } catch {
    return path.join(fs.realpathSync.native(parent), path.basename(absolute));
  }
}

function ownershipRecord(value: unknown): OwnershipRecord | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Partial<OwnershipRecord>;
  if (!Number.isSafeInteger(record.pid) || (record.pid ?? 0) <= 0) return null;
  if (typeof record.startedAt !== 'string' || !record.startedAt) return null;
  if (typeof record.token !== 'string' || !record.token) return null;
  if (typeof record.dbPath !== 'string' || !record.dbPath) return null;
  return record as OwnershipRecord;
}

function readOwnership(lockPath: string): OwnershipRecord | null {
  try {
    return ownershipRecord(JSON.parse(fs.readFileSync(lockPath, 'utf8')));
  } catch {
    return null;
  }
}

function processIsAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    return code !== 'ESRCH';
  }
}

function removeStaleOwnership(lockPath: string): void {
  try {
    fs.unlinkSync(lockPath);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
  }
}

function createOwnership(lockPath: string, record: OwnershipRecord): void {
  const tempPath = `${lockPath}.${record.pid}.${record.token}.tmp`;
  try {
    fs.writeFileSync(tempPath, `${JSON.stringify(record)}\n`, {
      encoding: 'utf8',
      flag: 'wx',
      mode: 0o600,
    });
    // The fully-written temp inode becomes the ownership path in one atomic
    // create-if-absent operation. A contender can therefore never mistake the
    // brief empty/partial-write window of an `open("wx")` lock for stale state.
    fs.linkSync(tempPath, lockPath);
  } finally {
    try {
      fs.unlinkSync(tempPath);
    } catch {
      // The uniquely-named temp link is non-authoritative. Never mask the
      // ownership result if best-effort cleanup cannot remove it.
    }
  }
}

export function acquireRuntimeOwnership(dbPath: string): RuntimeOwnershipHandle {
  const canonicalPath = canonicalDbPath(dbPath);
  const lockPath = `${canonicalPath}.runtime.lock`;
  const record: OwnershipRecord = {
    pid: process.pid,
    startedAt: new Date().toISOString(),
    token: crypto.randomUUID(),
    dbPath: canonicalPath,
  };

  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      createOwnership(lockPath, record);

      let released = false;
      return {
        dbPath: canonicalPath,
        lockPath,
        release() {
          if (released) return;
          released = true;
          const current = readOwnership(lockPath);
          if (current?.token !== record.token) return;
          removeStaleOwnership(lockPath);
        },
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      const existing = readOwnership(lockPath);
      if (existing && existing.dbPath === canonicalPath && processIsAlive(existing.pid)) {
        throw new RuntimeOwnershipError(existing);
      }
      removeStaleOwnership(lockPath);
    }
  }

  throw new Error(`Could not acquire runtime ownership for ${canonicalPath}`);
}
