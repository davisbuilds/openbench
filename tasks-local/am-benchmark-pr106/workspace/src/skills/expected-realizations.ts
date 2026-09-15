import { createHash } from 'node:crypto';
import type { Database } from 'better-sqlite3';
import type {
  ExpectedRealizationAssociationResponse,
  ExpectedRealizationCreateResponse,
  ExpectedRealizationValidationIssue,
  SkillExpectedPolicyArtifact,
  SkillExpectedProbePolicyArtifact,
  SkillExpectedRealization,
  SkillExpectedRealizationHarness,
  SkillExpectedRealizationInput,
  SkillExpectedRealizationMember,
  SkillExpectedRealizationProvenance,
  SkillExpectedVendorPolicyArtifact,
} from '../api/v2/types.js';
import {
  insertExpectedRealizationPersistenceRow,
  insertSessionExpectedRealizationAssociation,
  selectBrowsingSessionHarness,
  selectExpectedRealizationHarness,
  selectExpectedRealizationPersistenceRow,
  selectSessionExpectedRealizationId,
  selectSessionExpectedRealizationPersistenceRow,
  type ExpectedRealizationPersistenceRow,
} from '../db/v2-queries.js';

const MAX_ID_LENGTH = 256;
const MAX_TEXT_LENGTH = 512;
const MAX_URI_LENGTH = 2_048;
const MAX_PROFILE_COMPONENTS = 64;
const MAX_EXPECTED_SKILLS = 1_024;
const MAX_POLICY_ARTIFACTS = 32;
const MAX_CANONICAL_PAYLOAD_BYTES = 1_048_576;
const SHA256_PATTERN = /^(?:sha256:)?([a-f0-9]{64})$/i;
const ISO_TIMESTAMP_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/;

export type CreateExpectedRealizationResult = ExpectedRealizationCreateResponse;
export type AssociateExpectedRealizationResult = ExpectedRealizationAssociationResponse;

type CanonicalExpectedRealization = Omit<SkillExpectedRealization, 'contentHash'>;

interface ValidationSuccess {
  ok: true;
  realization: SkillExpectedRealization;
  canonicalJson: string;
}

interface ValidationFailure {
  ok: false;
  issues: ExpectedRealizationValidationIssue[];
}

type ValidationResult = ValidationSuccess | ValidationFailure;

function addIssue(
  issues: ExpectedRealizationValidationIssue[],
  path: string,
  code: string,
  message: string,
): void {
  issues.push({ path, code, message });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function boundedString(
  value: unknown,
  path: string,
  issues: ExpectedRealizationValidationIssue[],
  maxLength = MAX_TEXT_LENGTH,
): string | null {
  if (typeof value !== 'string' || value.trim().length === 0) {
    addIssue(issues, path, 'required_string', 'must be a non-empty string');
    return null;
  }
  const normalized = value.trim();
  if (normalized.length > maxLength) {
    addIssue(issues, path, 'string_too_long', `must be at most ${maxLength} characters`);
    return null;
  }
  return normalized;
}

function optionalBoundedString(
  value: unknown,
  path: string,
  issues: ExpectedRealizationValidationIssue[],
  maxLength = MAX_TEXT_LENGTH,
): string | null {
  if (value == null) return null;
  return boundedString(value, path, issues, maxLength);
}

function canonicalTimestamp(
  value: unknown,
  path: string,
  issues: ExpectedRealizationValidationIssue[],
): string | null {
  const text = boundedString(value, path, issues, 64);
  if (text === null) return null;
  const match = ISO_TIMESTAMP_PATTERN.exec(text);
  if (!match) {
    addIssue(issues, path, 'invalid_timestamp', 'must be an ISO-8601 timestamp with timezone');
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const offsetHour = match[8] === undefined ? 0 : Number(match[8]);
  const offsetMinute = match[9] === undefined ? 0 : Number(match[9]);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > (daysInMonth[month - 1] ?? 0) ||
    hour > 23 ||
    minute > 59 ||
    second > 59 ||
    offsetHour > 23 ||
    offsetMinute > 59
  ) {
    addIssue(issues, path, 'invalid_timestamp', 'must contain a valid calendar date and time');
    return null;
  }
  const epoch = Date.parse(text);
  if (!Number.isFinite(epoch)) {
    addIssue(issues, path, 'invalid_timestamp', 'must be an ISO-8601 timestamp');
    return null;
  }
  return new Date(epoch).toISOString();
}

function optionalCanonicalTimestamp(
  value: unknown,
  path: string,
  issues: ExpectedRealizationValidationIssue[],
): string | null {
  if (value == null) return null;
  return canonicalTimestamp(value, path, issues);
}

function contentHash(
  value: unknown,
  path: string,
  issues: ExpectedRealizationValidationIssue[],
): string | null {
  const text = boundedString(value, path, issues, 71);
  if (text === null) return null;
  const match = SHA256_PATTERN.exec(text);
  if (!match?.[1]) {
    addIssue(
      issues,
      path,
      'invalid_sha256',
      'must be a SHA-256 hex digest, with an optional sha256: prefix',
    );
    return null;
  }
  return match[1].toLowerCase();
}

function sourceUri(
  value: unknown,
  path: string,
  issues: ExpectedRealizationValidationIssue[],
): string | null {
  const text = boundedString(value, path, issues, MAX_URI_LENGTH);
  if (text === null) return null;
  try {
    return new URL(text).toString();
  } catch {
    addIssue(issues, path, 'invalid_uri', 'must be an absolute URI');
    return null;
  }
}

function stringArray(
  value: unknown,
  path: string,
  issues: ExpectedRealizationValidationIssue[],
  maxItems: number,
): string[] {
  if (!Array.isArray(value) || value.length === 0) {
    addIssue(issues, path, 'required_array', 'must be a non-empty array');
    return [];
  }
  if (value.length > maxItems) {
    addIssue(issues, path, 'array_too_large', `must contain at most ${maxItems} entries`);
  }
  const parsed = value.slice(0, maxItems).map((entry, index) =>
    boundedString(entry, `${path}[${index}]`, issues, MAX_ID_LENGTH)
  ).filter((entry): entry is string => entry !== null);
  const unique = new Set(parsed);
  if (unique.size !== parsed.length) {
    addIssue(issues, path, 'duplicate_entry', 'must not contain duplicate entries');
  }
  return [...unique].sort();
}

function expectedSkills(
  value: unknown,
  issues: ExpectedRealizationValidationIssue[],
): SkillExpectedRealizationMember[] {
  const path = 'skills';
  if (!Array.isArray(value) || value.length === 0) {
    addIssue(issues, path, 'required_array', 'must be a non-empty array');
    return [];
  }
  if (value.length > MAX_EXPECTED_SKILLS) {
    addIssue(
      issues,
      path,
      'array_too_large',
      `must contain at most ${MAX_EXPECTED_SKILLS} entries`,
    );
  }

  const skills: SkillExpectedRealizationMember[] = [];
  for (const [index, raw] of value.slice(0, MAX_EXPECTED_SKILLS).entries()) {
    const itemPath = `${path}[${index}]`;
    if (!isRecord(raw)) {
      addIssue(issues, itemPath, 'invalid_object', 'must be an object');
      continue;
    }
    const name = boundedString(raw['name'], `${itemPath}.name`, issues, MAX_ID_LENGTH);
    const descriptionFingerprint = contentHash(
      raw['descriptionFingerprint'],
      `${itemPath}.descriptionFingerprint`,
      issues,
    );
    const version = optionalBoundedString(
      raw['version'],
      `${itemPath}.version`,
      issues,
      MAX_ID_LENGTH,
    );
    const identity = optionalBoundedString(
      raw['contentIdentity'],
      `${itemPath}.contentIdentity`,
      issues,
      MAX_TEXT_LENGTH,
    );
    if (name !== null && descriptionFingerprint !== null) {
      skills.push({
        name,
        descriptionFingerprint,
        version,
        contentIdentity: identity,
      });
    }
  }

  const names = new Set(skills.map(skill => skill.name));
  if (names.size !== skills.length) {
    addIssue(issues, path, 'duplicate_skill_name', 'must not contain duplicate skill names');
  }
  return skills.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
}

function provenance(
  value: unknown,
  issues: ExpectedRealizationValidationIssue[],
): SkillExpectedRealizationProvenance | null {
  const path = 'provenance';
  if (!isRecord(value)) {
    addIssue(issues, path, 'invalid_object', 'must be an object');
    return null;
  }
  const producer = boundedString(value['producer'], `${path}.producer`, issues);
  const producerVersion = boundedString(
    value['producerVersion'],
    `${path}.producerVersion`,
    issues,
    MAX_ID_LENGTH,
  );
  const artifactId = boundedString(
    value['artifactId'],
    `${path}.artifactId`,
    issues,
    MAX_ID_LENGTH,
  );
  const artifactRevision = boundedString(
    value['artifactRevision'],
    `${path}.artifactRevision`,
    issues,
    MAX_ID_LENGTH,
  );
  const uri = sourceUri(value['sourceUri'], `${path}.sourceUri`, issues);
  if (
    producer === null ||
    producerVersion === null ||
    artifactId === null ||
    artifactRevision === null ||
    uri === null
  ) {
    return null;
  }
  return {
    producer,
    producerVersion,
    artifactId,
    artifactRevision,
    sourceUri: uri,
  };
}

function policyArtifacts(
  value: unknown,
  realizationHarness: SkillExpectedRealizationHarness | null,
  issues: ExpectedRealizationValidationIssue[],
): SkillExpectedPolicyArtifact[] {
  const path = 'policyArtifacts';
  if (value == null) return [];
  if (!Array.isArray(value)) {
    addIssue(issues, path, 'invalid_array', 'must be an array');
    return [];
  }
  if (value.length > MAX_POLICY_ARTIFACTS) {
    addIssue(
      issues,
      path,
      'array_too_large',
      `must contain at most ${MAX_POLICY_ARTIFACTS} entries`,
    );
  }

  const policies: SkillExpectedPolicyArtifact[] = [];
  for (const [index, raw] of value.slice(0, MAX_POLICY_ARTIFACTS).entries()) {
    const itemPath = `${path}[${index}]`;
    if (!isRecord(raw)) {
      addIssue(issues, itemPath, 'invalid_object', 'must be an object');
      continue;
    }
    const kind = raw['kind'];
    if (kind !== 'vendor_policy_snapshot' && kind !== 'version_scoped_probe') {
      addIssue(
        issues,
        `${itemPath}.kind`,
        'unsupported_policy_authority',
        'must be vendor_policy_snapshot or version_scoped_probe',
      );
      continue;
    }
    const artifactId = boundedString(
      raw['artifactId'],
      `${itemPath}.artifactId`,
      issues,
      MAX_ID_LENGTH,
    );
    const artifactRevision = boundedString(
      raw['artifactRevision'],
      `${itemPath}.artifactRevision`,
      issues,
      MAX_ID_LENGTH,
    );
    const hash = contentHash(raw['contentHash'], `${itemPath}.contentHash`, issues);
    const harness: SkillExpectedRealizationHarness | null =
      raw['harness'] === 'claude' || raw['harness'] === 'codex'
      ? raw['harness']
      : null;
    if (harness === null) {
      addIssue(
        issues,
        `${itemPath}.harness`,
        'invalid_harness',
        'must be claude or codex',
      );
    } else if (realizationHarness !== null && harness !== realizationHarness) {
      addIssue(
        issues,
        `${itemPath}.harness`,
        'policy_harness_mismatch',
        'must match the expected realization harness',
      );
    }
    const harnessVersion = boundedString(
      raw['harnessVersion'],
      `${itemPath}.harnessVersion`,
      issues,
      MAX_ID_LENGTH,
    );
    const model = optionalBoundedString(
      raw['model'],
      `${itemPath}.model`,
      issues,
      MAX_ID_LENGTH,
    );
    const modelVersion = optionalBoundedString(
      raw['modelVersion'],
      `${itemPath}.modelVersion`,
      issues,
      MAX_ID_LENGTH,
    );
    if (model === null && modelVersion !== null) {
      addIssue(
        issues,
        `${itemPath}.modelVersion`,
        'incomplete_model_identity',
        'modelVersion requires a model identifier',
      );
    }
    const contextWindowIdentity = boundedString(
      raw['contextWindowIdentity'],
      `${itemPath}.contextWindowIdentity`,
      issues,
      MAX_ID_LENGTH,
    );
    const runtimeRepresentation = boundedString(
      raw['runtimeRepresentation'],
      `${itemPath}.runtimeRepresentation`,
      issues,
    );
    const limitValue = typeof raw['limitValue'] === 'number' &&
      Number.isFinite(raw['limitValue']) &&
      raw['limitValue'] > 0
      ? raw['limitValue']
      : null;
    if (limitValue === null) {
      addIssue(
        issues,
        `${itemPath}.limitValue`,
        'invalid_limit',
        'must be a positive finite number',
      );
    }
    const limitUnit = boundedString(
      raw['limitUnit'],
      `${itemPath}.limitUnit`,
      issues,
      MAX_ID_LENGTH,
    );
    const measurementMethod = boundedString(
      raw['measurementMethod'],
      `${itemPath}.measurementMethod`,
      issues,
    );
    const observedAt = canonicalTimestamp(
      raw['observedAt'],
      `${itemPath}.observedAt`,
      issues,
    );
    const expiresAt = canonicalTimestamp(
      raw['expiresAt'],
      `${itemPath}.expiresAt`,
      issues,
    );
    if (
      observedAt !== null &&
      expiresAt !== null &&
      Date.parse(observedAt) >= Date.parse(expiresAt)
    ) {
      addIssue(
        issues,
        `${itemPath}.expiresAt`,
        'invalid_freshness_interval',
        'must be later than observedAt',
      );
    }
    const producer = boundedString(raw['producer'], `${itemPath}.producer`, issues);
    const producerVersion = boundedString(
      raw['producerVersion'],
      `${itemPath}.producerVersion`,
      issues,
      MAX_ID_LENGTH,
    );

    let authority:
      | Pick<SkillExpectedVendorPolicyArtifact, 'sourceUri'>
      | Pick<SkillExpectedProbePolicyArtifact, 'probeIdentity'>
      | null = null;
    if (kind === 'vendor_policy_snapshot') {
      const uri = sourceUri(raw['sourceUri'], `${itemPath}.sourceUri`, issues);
      if (raw['probeIdentity'] != null) {
        addIssue(
          issues,
          `${itemPath}.probeIdentity`,
          'conflicting_policy_source',
          'must be absent for vendor policy snapshots',
        );
      }
      if (uri !== null) authority = { sourceUri: uri };
    } else {
      const probeIdentity = boundedString(
        raw['probeIdentity'],
        `${itemPath}.probeIdentity`,
        issues,
      );
      if (raw['sourceUri'] != null) {
        addIssue(
          issues,
          `${itemPath}.sourceUri`,
          'conflicting_policy_source',
          'must be absent for version-scoped probes',
        );
      }
      if (probeIdentity !== null) authority = { probeIdentity };
    }

    if (
      artifactId === null ||
      artifactRevision === null ||
      hash === null ||
      harness === null ||
      harnessVersion === null ||
      contextWindowIdentity === null ||
      runtimeRepresentation === null ||
      limitValue === null ||
      limitUnit === null ||
      measurementMethod === null ||
      observedAt === null ||
      expiresAt === null ||
      producer === null ||
      producerVersion === null ||
      authority === null
    ) {
      continue;
    }

    const base = {
      artifactId,
      artifactRevision,
      contentHash: hash,
      harness,
      harnessVersion,
      model,
      modelVersion,
      contextWindowIdentity,
      runtimeRepresentation,
      limitValue,
      limitUnit,
      measurementMethod,
      observedAt,
      expiresAt,
      producer,
      producerVersion,
    };
    policies.push(kind === 'vendor_policy_snapshot'
      ? { kind, ...base, ...(authority as Pick<SkillExpectedVendorPolicyArtifact, 'sourceUri'>) }
      : { kind, ...base, ...(authority as Pick<SkillExpectedProbePolicyArtifact, 'probeIdentity'>) });
  }

  const artifactIds = new Set(policies.map(policy => policy.artifactId));
  if (artifactIds.size !== policies.length) {
    addIssue(
      issues,
      path,
      'duplicate_policy_artifact',
      'must not contain duplicate artifact IDs',
    );
  }
  return policies.sort((a, b) =>
    a.artifactId < b.artifactId ? -1 : a.artifactId > b.artifactId ? 1 : 0
  );
}

function validateExpectedRealization(input: unknown): ValidationResult {
  const issues: ExpectedRealizationValidationIssue[] = [];
  if (!isRecord(input)) {
    return {
      ok: false,
      issues: [{ path: '', code: 'invalid_object', message: 'must be an object' }],
    };
  }

  const id = boundedString(input['id'], 'id', issues, MAX_ID_LENGTH);
  const harness = input['harness'] === 'claude' || input['harness'] === 'codex'
    ? input['harness']
    : null;
  if (harness === null) {
    addIssue(issues, 'harness', 'invalid_harness', 'must be claude or codex');
  }
  const profileIdentity = boundedString(
    input['profileIdentity'],
    'profileIdentity',
    issues,
  );
  const profileComposition = stringArray(
    input['profileComposition'],
    'profileComposition',
    issues,
    MAX_PROFILE_COMPONENTS,
  );
  const canonicalRevision = boundedString(
    input['canonicalRevision'],
    'canonicalRevision',
    issues,
  );
  const validFrom = canonicalTimestamp(input['validFrom'], 'validFrom', issues);
  const validTo = optionalCanonicalTimestamp(input['validTo'], 'validTo', issues);
  if (
    validFrom !== null &&
    validTo !== null &&
    Date.parse(validFrom) >= Date.parse(validTo)
  ) {
    addIssue(issues, 'validTo', 'invalid_validity_interval', 'must be later than validFrom');
  }
  const skills = expectedSkills(input['skills'], issues);
  const policies = policyArtifacts(input['policyArtifacts'], harness, issues);
  const realizationProvenance = provenance(input['provenance'], issues);

  if (
    issues.length > 0 ||
    id === null ||
    harness === null ||
    profileIdentity === null ||
    canonicalRevision === null ||
    validFrom === null ||
    realizationProvenance === null
  ) {
    return { ok: false, issues };
  }

  const canonical: CanonicalExpectedRealization = {
    id,
    harness,
    profileIdentity,
    profileComposition,
    canonicalRevision,
    validFrom,
    validTo,
    skills,
    policyArtifacts: policies,
    provenance: realizationProvenance,
  };
  const canonicalJson = JSON.stringify(canonical);
  if (Buffer.byteLength(canonicalJson, 'utf8') > MAX_CANONICAL_PAYLOAD_BYTES) {
    return {
      ok: false,
      issues: [{
        path: '',
        code: 'payload_too_large',
        message: `canonical payload must be at most ${MAX_CANONICAL_PAYLOAD_BYTES} bytes`,
      }],
    };
  }
  const hash = createHash('sha256').update(canonicalJson, 'utf8').digest('hex');
  return {
    ok: true,
    realization: { ...canonical, contentHash: hash },
    canonicalJson,
  };
}

function hydrateExpectedRealization(
  row: ExpectedRealizationPersistenceRow,
): SkillExpectedRealization {
  const payload = JSON.parse(row.payload_json) as CanonicalExpectedRealization;
  return { ...payload, contentHash: row.content_hash };
}

export function verifyPersistedExpectedRealization(
  payloadJson: string,
  expectedContentHash: string,
): SkillExpectedRealization | null {
  if (!/^[a-f0-9]{64}$/.test(expectedContentHash)) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(payloadJson) as unknown;
  } catch {
    return null;
  }
  const validation = validateExpectedRealization(payload);
  if (
    !validation.ok
    || validation.canonicalJson !== payloadJson
    || validation.realization.contentHash !== expectedContentHash
  ) {
    return null;
  }
  return validation.realization;
}

export function createExpectedRealization(
  db: Database,
  input: SkillExpectedRealizationInput | unknown,
): CreateExpectedRealizationResult {
  const validation = validateExpectedRealization(input);
  if (!validation.ok) {
    return {
      ok: false,
      status: 'invalid',
      code: 'invalid_expected_realization',
      issues: validation.issues,
    };
  }

  return db.transaction((): CreateExpectedRealizationResult => {
    const existing = selectExpectedRealizationPersistenceRow(
      db,
      validation.realization.id,
    );
    if (existing) {
      if (
        existing.content_hash === validation.realization.contentHash &&
        existing.payload_json === validation.canonicalJson
      ) {
        return {
          ok: true,
          status: 'replayed',
          realization: hydrateExpectedRealization(existing),
        };
      }
      return {
        ok: false,
        status: 'conflict',
        code: 'expected_realization_content_conflict',
        existingContentHash: existing.content_hash,
      };
    }

    insertExpectedRealizationPersistenceRow(db, {
      id: validation.realization.id,
      harness: validation.realization.harness,
      profileIdentity: validation.realization.profileIdentity,
      canonicalRevision: validation.realization.canonicalRevision,
      validFrom: validation.realization.validFrom,
      validTo: validation.realization.validTo,
      canonicalJson: validation.canonicalJson,
      contentHash: validation.realization.contentHash,
    });
    return {
      ok: true,
      status: 'created',
      realization: validation.realization,
    };
  })();
}

export function associateExpectedRealization(
  db: Database,
  sessionIdInput: unknown,
  realizationIdInput: unknown,
): AssociateExpectedRealizationResult {
  const issues: ExpectedRealizationValidationIssue[] = [];
  const sessionId = boundedString(
    sessionIdInput,
    'sessionId',
    issues,
    MAX_ID_LENGTH,
  );
  const realizationId = boundedString(
    realizationIdInput,
    'realizationId',
    issues,
    MAX_ID_LENGTH,
  );
  if (sessionId === null || realizationId === null) {
    return {
      ok: false,
      status: 'invalid',
      code: 'invalid_association',
      issues,
    };
  }

  return db.transaction((): AssociateExpectedRealizationResult => {
    const sessionHarness = selectBrowsingSessionHarness(db, sessionId);
    if (sessionHarness === undefined) {
      return { ok: false, status: 'not_found', code: 'session_not_found' };
    }
    const realizationHarness = selectExpectedRealizationHarness(db, realizationId);
    if (realizationHarness === undefined) {
      return {
        ok: false,
        status: 'not_found',
        code: 'expected_realization_not_found',
      };
    }
    if (sessionHarness !== realizationHarness) {
      return {
        ok: false,
        status: 'unprocessable',
        code: 'expected_realization_harness_mismatch',
        sessionHarness,
        realizationHarness,
      };
    }

    const existingRealizationId = selectSessionExpectedRealizationId(db, sessionId);
    if (existingRealizationId !== undefined) {
      if (existingRealizationId === realizationId) {
        return {
          ok: true,
          status: 'replayed',
          sessionId,
          realizationId,
        };
      }
      return {
        ok: false,
        status: 'conflict',
        code: 'session_expected_realization_conflict',
        existingRealizationId,
      };
    }

    insertSessionExpectedRealizationAssociation(db, sessionId, realizationId);
    return {
      ok: true,
      status: 'associated',
      sessionId,
      realizationId,
    };
  })();
}

export function getExpectedRealization(
  db: Database,
  realizationId: string,
): SkillExpectedRealization | null {
  const row = selectExpectedRealizationPersistenceRow(db, realizationId);
  return row ? hydrateExpectedRealization(row) : null;
}

export function getSessionExpectedRealization(
  db: Database,
  sessionId: string,
): SkillExpectedRealization | null {
  const row = selectSessionExpectedRealizationPersistenceRow(db, sessionId);
  return row ? hydrateExpectedRealization(row) : null;
}
