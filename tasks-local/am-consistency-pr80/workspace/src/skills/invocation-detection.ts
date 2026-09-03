import { createHash } from 'node:crypto';

function parseJson(value: string | null | undefined): unknown {
  if (!value) return undefined;
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

export function extractCanonicalCodexSessionId(sessionId: string): string {
  const match = sessionId.match(/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i);
  return match?.[1] ?? sessionId;
}

export function extractExplicitSkillName(inputJson: string | null): string | undefined {
  const record = asRecord(parseJson(inputJson));
  const skill = record?.['skill'];
  return typeof skill === 'string' && skill.trim() ? skill.trim() : undefined;
}

export function extractCodexCommandFromInputJson(inputJson: string | null): string | undefined {
  const parsed = parseJson(inputJson);
  if (typeof parsed === 'string') return parsed;

  const record = asRecord(parsed);
  if (!record) return undefined;
  const cmd = record['cmd'];
  if (typeof cmd === 'string') return cmd;
  const command = record['command'];
  return typeof command === 'string' ? command : undefined;
}

export function extractCodexCommandFromEventMetadata(metadataJson: string | null): string | undefined {
  const record = asRecord(parseJson(metadataJson));
  if (!record) return undefined;

  const argumentsValue = record['arguments'];
  if (typeof argumentsValue === 'string') return argumentsValue;

  const argumentsRecord = asRecord(argumentsValue);
  if (argumentsRecord) {
    const cmd = argumentsRecord['cmd'];
    if (typeof cmd === 'string') return cmd;
    const command = argumentsRecord['command'];
    if (typeof command === 'string') return command;
  }

  const input = record['input'];
  return typeof input === 'string' ? input : undefined;
}

export function extractCodexSkillNamesFromCommand(command: string): string[] {
  const skillNames = new Set<string>();
  const pattern = /(?:^|[\s'"])(~?\/[^\s'"]*\/([^/\s'"]+)\/SKILL\.md)(?=$|[\s'"])/g;
  const placeholderCharacters = ['$', '*', '?', '[', ']', '{', '}'];

  for (const match of command.matchAll(pattern)) {
    const skillName = match[2]?.trim();
    const isConcreteSkill = skillName
      && skillName !== '.'
      && skillName !== '..'
      && !placeholderCharacters.some(character => skillName.includes(character));
    if (isConcreteSkill) skillNames.add(skillName);
  }

  return [...skillNames];
}

export function fingerprintCodexCommand(command: string): string {
  return createHash('sha256').update(command, 'utf8').digest('hex');
}
