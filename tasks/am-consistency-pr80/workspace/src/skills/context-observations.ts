import { createHash } from 'node:crypto';
import path from 'node:path';

export type SessionContextObservationKind =
  | 'consultation'
  | 'compaction'
  | 'catalog_presentation'
  | 'instruction_load';

export interface CatalogObservationEntry {
  name: string;
  description: string | null;
  descriptionFingerprint: string | null;
  sourceLocation: string | null;
  scope: string | null;
}

export interface SessionContextObservation {
  ordinal: number;
  kind: SessionContextObservationKind;
  source: string;
  timestamp: string | null;
  skillName?: string;
  commandFingerprint?: string;
  projectIdentity?: string;
  reason?: string;
  metadata?: Record<string, unknown>;
  catalogEntries?: CatalogObservationEntry[];
}

export interface ObservationCapability {
  observable: boolean;
  reason?: string;
}

export interface SkillContextCapabilities {
  orderedConsultations: ObservationCapability;
  compactionVisibility: ObservationCapability;
  catalogPresentation: ObservationCapability;
  instructionLoads: ObservationCapability;
  diagnostics: string[];
}

export interface ParsedSkillContext {
  projectIdentity: string | null;
  observations: SessionContextObservation[];
  capabilities: SkillContextCapabilities;
}

function sha256(value: string): string {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

export function projectIdentityFromCwd(cwd: string | null | undefined): string | null {
  if (!cwd?.trim()) return null;
  const normalized = path.posix.normalize(cwd.trim().replaceAll('\\', '/'));
  return `cwd:${sha256(normalized)}`;
}

function decodeXmlText(value: string): string {
  return value
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&quot;', '"')
    .replaceAll('&apos;', "'")
    .replaceAll('&amp;', '&')
    .trim();
}

function xmlValue(block: string, tag: string): string | null {
  const match = block.match(new RegExp(`<${tag}>([\\s\\S]*?)<\\/${tag}>`, 'i'));
  return match ? decodeXmlText(match[1] ?? '') : null;
}

function markdownCatalogEntries(body: string): CatalogObservationEntry[] {
  const logicalLines: string[] = [];
  for (const rawLine of body.split('\n')) {
    const line = rawLine.trim();
    if (/^-\s+/.test(line)) {
      logicalLines.push(line);
    } else if (
      logicalLines.length > 0
      && line
      && !line.startsWith('#')
      && !line.startsWith('<')
    ) {
      logicalLines[logicalLines.length - 1] += ` ${line}`;
    }
  }

  const entries: CatalogObservationEntry[] = [];
  const pattern = /^-\s+([A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*):\s+(.+?)\s+\(file:\s*(.+?)\)\s*$/;
  for (const line of logicalLines) {
    const match = line.match(pattern);
    if (!match) continue;
    const name = match[1]!;
    const description = match[2]!.trim();
    entries.push({
      name,
      description,
      descriptionFingerprint: sha256(description),
      sourceLocation: match[3]!.trim(),
      scope: null,
    });
  }
  return entries;
}

export interface ParsedCatalogPresentation {
  retainedBlock: string;
  fingerprint: string;
  representation: 'skills_instructions_xml' | 'skills_instructions_markdown';
  measurement: {
    value: number;
    unit: 'utf8_bytes';
    method: 'retained_catalog_block_utf8_bytes/v1';
  };
  truncation: 'observed' | 'not_observed' | 'unknown';
  entries: CatalogObservationEntry[];
}

export function parseCodexCatalogPresentations(text: string): ParsedCatalogPresentation[] {
  const presentations: ParsedCatalogPresentation[] = [];
  const pattern = /<skills_instructions(?:\s[^>]*)?>([\s\S]*?)<\/skills_instructions>/gi;
  for (const match of text.matchAll(pattern)) {
    const retainedBlock = match[0];
    const body = match[1] ?? '';
    const entries: CatalogObservationEntry[] = [];
    for (const skillMatch of body.matchAll(/<skill>([\s\S]*?)<\/skill>/gi)) {
      const skillBlock = skillMatch[1] ?? '';
      const name = xmlValue(skillBlock, 'name');
      if (!name) continue;
      const description = xmlValue(skillBlock, 'description');
      entries.push({
        name,
        description,
        descriptionFingerprint: description === null ? null : sha256(description),
        sourceLocation: xmlValue(skillBlock, 'location'),
        scope: xmlValue(skillBlock, 'scope'),
      });
    }
    if (entries.length === 0) entries.push(...markdownCatalogEntries(body));
    const representation = /<skill>([\s\S]*?)<\/skill>/i.test(body)
      || /<skills(?:\s[^>]*)?>\s*<\/skills>/i.test(body)
      ? 'skills_instructions_xml'
      : 'skills_instructions_markdown';
    const explicitlyEmpty = /<skills(?:\s[^>]*)?>\s*<\/skills>/i.test(body)
      || /available skills\s*:\s*(?:none|\[\])/i.test(body);
    if (entries.length === 0 && !explicitlyEmpty) continue;
    const canonical = JSON.stringify(entries.map((entry, ordinal) => ({
      ordinal,
      name: entry.name,
      description: entry.description,
      sourceLocation: entry.sourceLocation,
      sourceScope: entry.scope,
    })));
    const lower = retainedBlock.toLowerCase();
    const truncation = /truncat(?:ed|ion)\s*[:=]\s*(?:true|yes|1)/.test(lower)
      ? 'observed'
      : /truncat(?:ed|ion)\s*[:=]\s*(?:false|no|0)/.test(lower)
        ? 'not_observed'
        : 'unknown';
    presentations.push({
      retainedBlock,
      fingerprint: sha256(canonical),
      representation,
      measurement: {
        value: Buffer.byteLength(retainedBlock, 'utf8'),
        unit: 'utf8_bytes',
        method: 'retained_catalog_block_utf8_bytes/v1',
      },
      truncation,
      entries,
    });
  }
  return presentations;
}

export function unavailableSkillContext(reason = 'harness_signal_unavailable'): ParsedSkillContext {
  const unavailable = (): ObservationCapability => ({ observable: false, reason });
  return {
    projectIdentity: null,
    observations: [],
    capabilities: {
      orderedConsultations: unavailable(),
      compactionVisibility: unavailable(),
      catalogPresentation: unavailable(),
      instructionLoads: unavailable(),
      diagnostics: [],
    },
  };
}
