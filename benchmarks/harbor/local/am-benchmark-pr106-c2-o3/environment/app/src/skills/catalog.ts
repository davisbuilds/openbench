import fs from 'node:fs';
import path from 'node:path';
import type { Database } from 'better-sqlite3';

export interface CatalogSkill {
  name: string;
  version: string | null;
  /** The catalog directory this skill was resolved from (not the skill dir). */
  dir: string;
}

export interface CatalogSnapshot {
  name: string;
  version: string | null;
  firstSeenAt: string;
  lastSeenAt: string;
}

export interface ResolvedVersion {
  version: string | null;
  /** True when the timestamp fell outside every observed window and the version
   *  is an approximation (the earliest known version for that skill). */
  approximate: boolean;
}

const FRONTMATTER_RE = /^---\n([\s\S]*?)\n---/;
const VERSION_LINE_RE = /^version:\s*(.+?)\s*$/m;

function extractVersion(skillMd: string): string | null {
  const frontmatter = FRONTMATTER_RE.exec(skillMd);
  if (!frontmatter) return null;

  const match = VERSION_LINE_RE.exec(frontmatter[1]);
  if (!match) return null;

  return match[1].replace(/^['"]|['"]$/g, '').trim() || null;
}

function readSkill(catalogDir: string, entryName: string): CatalogSkill | null {
  const skillMdPath = path.join(catalogDir, entryName, 'SKILL.md');
  let content: string;
  try {
    // readFileSync follows symlinks, so symlinked skill dirs resolve transparently.
    content = fs.readFileSync(skillMdPath, 'utf-8');
  } catch {
    return null;
  }

  return { name: entryName, version: extractVersion(content), dir: catalogDir };
}

/**
 * Enumerate installed skills across the given catalog directories, reading each
 * skill's declared version from its SKILL.md frontmatter. Skills are deduped by
 * name with earlier catalog dirs winning; unreadable entries and missing
 * directories are skipped rather than throwing.
 */
export function scanSkillCatalogs(dirs: string[]): CatalogSkill[] {
  const skills: CatalogSkill[] = [];
  const seen = new Set<string>();

  for (const dir of dirs) {
    let entries: string[];
    try {
      entries = fs.readdirSync(dir);
    } catch {
      continue;
    }

    for (const entryName of entries) {
      if (seen.has(entryName)) continue;
      const skill = readSkill(dir, entryName);
      if (!skill) continue;
      seen.add(entryName);
      skills.push(skill);
    }
  }

  return skills;
}

/**
 * Stamp the current catalog state into `skill_catalog_snapshots`: for each
 * observed (name, version) pair, extend `last_seen_at` to `now` if the pair is
 * already known, otherwise insert it with `first_seen_at = last_seen_at = now`.
 * Uniqueness is enforced here (via a NULL-safe `IS` match) rather than by the
 * table's primary key, because a version-less skill's NULL version cannot key a
 * NULL-distinct primary key.
 */
export function refreshCatalogSnapshots(db: Database, skills: CatalogSkill[], now: string): void {
  const select = db.prepare(
    'SELECT rowid FROM skill_catalog_snapshots WHERE name = ? AND version IS ?',
  );
  const update = db.prepare('UPDATE skill_catalog_snapshots SET last_seen_at = ? WHERE rowid = ?');
  const insert = db.prepare(
    'INSERT INTO skill_catalog_snapshots (name, version, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)',
  );

  const apply = db.transaction((rows: CatalogSkill[]) => {
    for (const { name, version } of rows) {
      const existing = select.get(name, version) as { rowid: number } | undefined;
      if (existing) {
        update.run(now, existing.rowid);
      } else {
        insert.run(name, version, now, now);
      }
    }
  });

  apply(skills);
}

/**
 * Parse a stored timestamp to epoch millis for comparison. Handles both ISO
 * (`2026-07-09T18:00:00.000Z`, from parsed message timestamps and snapshot
 * writes) and SQLite `datetime('now')` output (`2026-07-09 18:00:00`, UTC but
 * space-separated and Z-less, which `events.created_at` uses). Lexicographic
 * comparison of the two formats is wrong (space sorts before `T`), so both sides
 * must be normalized before comparing. Returns NaN for unparseable input.
 */
function toEpochMs(timestamp: string): number {
  const iso = timestamp.includes('T') ? timestamp : `${timestamp.replace(' ', 'T')}Z`;
  return Date.parse(iso);
}

/**
 * Resolve the skill version that was installed at `timestamp`, given the full
 * snapshot set. Returns the version whose observed window covers the timestamp;
 * failing that (e.g. an invocation predating the first snapshot), the earliest
 * known version for that skill, flagged approximate. Unknown skills resolve to a
 * null version.
 */
export function resolveVersionAt(
  snapshots: CatalogSnapshot[],
  name: string,
  timestamp: string,
): ResolvedVersion {
  const rows = snapshots.filter(s => s.name === name);
  if (rows.length === 0) return { version: null, approximate: false };

  const t = toEpochMs(timestamp);
  const covering = Number.isNaN(t)
    ? []
    : rows
      .filter(s => toEpochMs(s.firstSeenAt) <= t && t <= toEpochMs(s.lastSeenAt))
      .sort((a, b) => toEpochMs(b.firstSeenAt) - toEpochMs(a.firstSeenAt));
  if (covering.length > 0) {
    return { version: covering[0].version, approximate: false };
  }

  const earliest = rows.reduce((a, b) => (toEpochMs(a.firstSeenAt) <= toEpochMs(b.firstSeenAt) ? a : b));
  return { version: earliest.version, approximate: true };
}
