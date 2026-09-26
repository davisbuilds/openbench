import fs from 'node:fs';
import path from 'node:path';
import Database from 'better-sqlite3';

export class DatabaseBackupPolicyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'DatabaseBackupPolicyError';
  }
}

export interface DatabaseBackupResult {
  status: 'ok';
  output: string;
  bytes: number;
  total_pages: number;
  journal_mode: 'delete';
  integrity_check: 'ok';
  foreign_key_violations: number;
  replaced: boolean;
}

interface DatabaseBackupOptions {
  source: string;
  output: string;
  replace: boolean;
}

function policyFailure(message: string): never {
  throw new DatabaseBackupPolicyError(message);
}

function assertPrivateOwnedDirectory(directory: string): string {
  let info: fs.Stats;
  try {
    info = fs.lstatSync(directory);
  } catch {
    policyFailure('Backup output parent directory does not exist.');
  }
  if (info.isSymbolicLink()) policyFailure('Backup output parent directory must not be a symbolic link.');
  if (!info.isDirectory()) policyFailure('Backup output parent must be a directory.');
  if ((info.mode & 0o077) !== 0) policyFailure('Backup output parent directory must be private (mode 0700 or stricter).');
  if (typeof process.getuid === 'function' && info.uid !== process.getuid()) {
    policyFailure('Backup output parent directory must be owned by the current user.');
  }
  return fs.realpathSync(directory);
}

function inspectExistingOutput(output: string, replace: boolean): boolean {
  let info: fs.Stats;
  try {
    info = fs.lstatSync(output);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw error;
  }
  if (info.isSymbolicLink()) policyFailure('Backup output must not be a symbolic link.');
  if (!info.isFile()) policyFailure('Backup output must be a regular file.');
  if (!replace) policyFailure('Backup output already exists; pass --replace to publish a new copy atomically.');
  return true;
}

function assertNoSidecars(output: string): void {
  for (const suffix of ['-wal', '-shm', '-journal']) {
    if (fs.existsSync(`${output}${suffix}`)) {
      policyFailure('Backup output has an adjacent SQLite sidecar; close and review it before replacement.');
    }
  }
}

function resolveSource(source: string): string {
  const absolute = path.resolve(source);
  let info: fs.Stats;
  try {
    info = fs.statSync(absolute);
  } catch {
    throw new Error('Source database is unavailable.');
  }
  if (!info.isFile()) throw new Error('Source database is not a regular file.');
  return fs.realpathSync(absolute);
}

function assertDistinctFileIdentity(output: string, protectedPaths: string[]): void {
  let outputInfo: fs.Stats;
  try {
    outputInfo = fs.lstatSync(output);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return;
    throw error;
  }
  if (outputInfo.isSymbolicLink()) return;

  for (const protectedPath of protectedPaths) {
    let protectedInfo: fs.Stats;
    try {
      protectedInfo = fs.statSync(protectedPath);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') continue;
      throw error;
    }
    if (outputInfo.dev === protectedInfo.dev && outputInfo.ino === protectedInfo.ino) {
      policyFailure('Backup output must not overlap the source database or its sidecars.');
    }
  }
}

function resolveOutput(source: string, requested: string): string {
  if (!path.isAbsolute(requested)) policyFailure('Backup output path must be absolute.');
  const parent = assertPrivateOwnedDirectory(path.dirname(requested));
  const output = path.join(parent, path.basename(requested));
  const protectedPaths = [
    source,
    `${source}-wal`,
    `${source}-shm`,
    `${source}-journal`,
    `${source}.runtime.lock`,
  ];
  if (protectedPaths.includes(output)) {
    policyFailure('Backup output must not overlap the source database or its sidecars.');
  }
  assertDistinctFileIdentity(output, protectedPaths);
  return output;
}

function validateClosedBackup(
  filename: string,
): Pick<DatabaseBackupResult, 'journal_mode' | 'integrity_check' | 'foreign_key_violations'> {
  const backup = new Database(filename, { fileMustExist: true });
  try {
    const journalMode = backup.pragma('journal_mode = DELETE', { simple: true });
    if (journalMode !== 'delete') throw new Error('Exported database could not enter closed journal mode.');
    const integrity = backup.pragma('integrity_check', { simple: true });
    if (integrity !== 'ok') throw new Error('Exported database failed PRAGMA integrity_check.');
    const foreignKeyViolations = (backup.pragma('foreign_key_check') as unknown[]).length;
    if (foreignKeyViolations > 0) throw new Error('Exported database failed PRAGMA foreign_key_check.');
    return {
      journal_mode: 'delete',
      integrity_check: 'ok',
      foreign_key_violations: foreignKeyViolations,
    };
  } finally {
    backup.close();
  }
}

export async function createValidatedDatabaseBackup(options: DatabaseBackupOptions): Promise<DatabaseBackupResult> {
  const source = resolveSource(options.source);
  const output = resolveOutput(source, options.output);
  const replaced = inspectExistingOutput(output, options.replace);
  assertNoSidecars(output);

  const tempDirectory = fs.mkdtempSync(path.join(path.dirname(output), `.${path.basename(output)}.backup-`));
  fs.chmodSync(tempDirectory, 0o700);
  const tempOutput = path.join(tempDirectory, path.basename(output));
  const descriptor = fs.openSync(tempOutput, 'wx', 0o600);
  fs.closeSync(descriptor);

  try {
    const sourceDb = new Database(source, { readonly: true, fileMustExist: true });
    let metadata: Database.BackupMetadata;
    try {
      metadata = await sourceDb.backup(tempOutput);
    } finally {
      sourceDb.close();
    }

    fs.chmodSync(tempOutput, 0o600);
    const validation = validateClosedBackup(tempOutput);
    assertNoSidecars(tempOutput);
    if (options.replace) {
      fs.renameSync(tempOutput, output);
    } else {
      fs.linkSync(tempOutput, output);
      fs.unlinkSync(tempOutput);
    }
    fs.chmodSync(output, 0o600);

    return {
      status: 'ok',
      output,
      bytes: fs.statSync(output).size,
      total_pages: metadata.totalPages,
      ...validation,
      replaced,
    };
  } finally {
    fs.rmSync(tempDirectory, { recursive: true, force: true });
  }
}
