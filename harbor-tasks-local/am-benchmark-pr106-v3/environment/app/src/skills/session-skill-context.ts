import { createHash } from 'node:crypto';
import type Database from 'better-sqlite3';
import type {
  SessionExpectedRealizationState,
  SessionSkillContext,
  SessionSkillContextObservation,
  SkillCatalogBudget,
  SkillCatalogPresentationEntry,
  SkillCatalogPresentationMeasurement,
  SkillCatalogPresentationOccurrence,
  SkillCatalogPresentationState,
  SkillConsultationClass,
  SkillExpectedComparison,
  SkillExpectedRealization,
  SkillExpectedRealizationHarness,
  SkillExpectedRealizationReference,
  SkillInstructionLoadOccurrence,
  SkillInstructionReachState,
} from '../api/v2/types.js';
import {
  selectSessionExpectedRealizationPersistenceRow,
  selectSessionSkillContextCatalogEntries,
  selectSessionSkillContextInstructionEvents,
  selectSessionSkillContextObservations,
  selectSessionSkillContextSession,
  type ExpectedRealizationPersistenceRow,
  type SessionSkillContextCatalogEntryRow,
  type SessionSkillContextInstructionEventRow,
  type SessionSkillContextObservationRow,
  type SessionSkillContextSessionRow,
} from '../db/v2-queries.js';
import { verifyPersistedExpectedRealization } from './expected-realizations.js';

const MAX_OBSERVATIONS = 4_096;
const MAX_CATALOG_ENTRIES = 16_384;
const MAX_INSTRUCTION_EVENTS = 4_096;
const CATALOG_MEASUREMENT_METHOD = 'retained_catalog_block_utf8_bytes/v1';
const LEGACY_CATALOG_MEASUREMENT_METHOD = 'skill_catalog_presentation/v1';
const ACTIVE_SESSION_STATUSES = new Set(['live', 'active', 'available']);

interface StoredCapabilities {
  orderedConsultations?: { observable?: boolean; reason?: string };
  compactionVisibility?: { observable?: boolean; reason?: string };
  catalogPresentation?: { observable?: boolean; reason?: string };
  instructionLoads?: { observable?: boolean; reason?: string };
}

interface OccurrenceRuntime {
  harnessVersion: string | null;
  model: string | null;
  modelVersion: string | null;
  contextWindowIdentity: string | null;
  representation: string | null;
}

interface ParsedExpectedRealization {
  realization: SkillExpectedRealization;
  reference: SkillExpectedRealizationReference;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function parseRecord(value: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(value) as unknown;
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function sha256(value: string): string {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function parseCapabilities(value: string | null): StoredCapabilities {
  if (!value) return {};
  const parsed = parseRecord(value);
  return parsed ?? {};
}

function classificationCapability(capabilities: StoredCapabilities): {
  observable: boolean;
  reason?: string;
} {
  if (capabilities.orderedConsultations?.observable !== true) {
    return {
      observable: false,
      reason: capabilities.orderedConsultations?.reason
        ?? 'consultation_detection_unavailable',
    };
  }
  if (capabilities.compactionVisibility?.observable !== true) {
    return {
      observable: false,
      reason: capabilities.compactionVisibility?.reason
        ?? 'compaction_visibility_unavailable',
    };
  }
  return { observable: true };
}

function classifyObservations(
  rows: SessionSkillContextObservationRow[],
  observable: boolean,
): Map<number, SkillConsultationClass> {
  const classes = new Map<number, SkillConsultationClass>();
  const lastGeneration = new Map<string, number>();
  let generation = 0;
  for (const row of rows) {
    if (row.kind === 'compaction') {
      generation++;
      continue;
    }
    if (row.kind !== 'consultation' || !row.skill_name) continue;
    if (!observable) {
      classes.set(row.id, 'unclassifiable');
      continue;
    }
    const priorGeneration = lastGeneration.get(row.skill_name);
    const classification = priorGeneration === undefined
      ? 'first_read'
      : priorGeneration < generation
        ? 'rehydration_after_compaction'
        : 'repeat_no_compaction';
    classes.set(row.id, classification);
    lastGeneration.set(row.skill_name, generation);
  }
  return classes;
}

function projectObservations(
  rows: SessionSkillContextObservationRow[],
  consultationClasses: Map<number, SkillConsultationClass>,
): SessionSkillContextObservation[] {
  return rows.map(row => ({
    id: row.id,
    ordinal: row.ordinal,
    kind: row.kind as SessionSkillContextObservation['kind'],
    source: row.source,
    observedAt: row.observed_at,
    skillName: row.skill_name,
    commandFingerprint: row.command_fingerprint,
    projectIdentity: row.project_identity,
    reason: row.reason,
    consultationClass: consultationClasses.get(row.id) ?? null,
  }));
}

function parseExpectedRealization(
  row: ExpectedRealizationPersistenceRow | undefined,
  harness: SkillExpectedRealizationHarness,
): ParsedExpectedRealization | null {
  if (!row) return null;
  const realization = verifyPersistedExpectedRealization(
    row.payload_json,
    row.content_hash,
  );
  if (!realization || realization.harness !== harness) return null;
  return {
    realization,
    reference: {
      id: realization.id,
      contentHash: realization.contentHash,
      profileIdentity: realization.profileIdentity,
      canonicalRevision: realization.canonicalRevision,
      validFrom: realization.validFrom,
      validTo: realization.validTo,
      provenance: realization.provenance,
    },
  };
}

function expectedState(
  row: ExpectedRealizationPersistenceRow | undefined,
  parsed: ParsedExpectedRealization | null,
): SessionExpectedRealizationState {
  if (!row) {
    return { status: 'unavailable', reason: 'no_expected_realization' };
  }
  if (!parsed) {
    return { status: 'unavailable', reason: 'invalid_expected_realization' };
  }
  return { status: 'associated', realization: parsed.reference };
}

function catalogEntriesByObservation(
  rows: SessionSkillContextCatalogEntryRow[],
): Map<number, SkillCatalogPresentationEntry[]> {
  const result = new Map<number, SkillCatalogPresentationEntry[]>();
  for (const row of rows) {
    const entries = result.get(row.observation_id) ?? [];
    entries.push({
      ordinal: row.ordinal,
      name: row.skill_name,
      description: row.description,
      descriptionFingerprint: row.description === null ? null : sha256(row.description),
      sourceLocation: row.source_location,
      sourceScope: row.scope,
    });
    result.set(row.observation_id, entries);
  }
  return result;
}

function catalogFingerprint(entries: SkillCatalogPresentationEntry[]): string {
  return sha256(JSON.stringify(entries.map(entry => ({
    ordinal: entry.ordinal,
    name: entry.name,
    description: entry.description,
    sourceLocation: entry.sourceLocation,
    sourceScope: entry.sourceScope,
  }))));
}

function catalogMeasurement(
  metadata: Record<string, unknown>,
): SkillCatalogPresentationMeasurement | null {
  const measurement = metadata['measurement'];
  if (!isRecord(measurement)) return null;
  const value = measurement['value'];
  const unit = measurement['unit'];
  const method = measurement['method'];
  if (
    typeof value !== 'number'
    || !Number.isFinite(value)
    || value < 0
    || unit !== 'utf8_bytes'
    || (
      method !== CATALOG_MEASUREMENT_METHOD
      && method !== LEGACY_CATALOG_MEASUREMENT_METHOD
    )
  ) {
    return null;
  }
  return {
    value,
    unit,
    method: CATALOG_MEASUREMENT_METHOD,
    exact: true,
  };
}

function occurrenceRuntime(metadata: Record<string, unknown>): OccurrenceRuntime {
  const runtime = metadata['runtime'];
  if (!isRecord(runtime)) {
    return {
      harnessVersion: null,
      model: null,
      modelVersion: null,
      contextWindowIdentity: null,
      representation: null,
    };
  }
  return {
    harnessVersion: optionalString(runtime['harnessVersion']),
    model: optionalString(runtime['model']),
    modelVersion: optionalString(runtime['modelVersion']),
    contextWindowIdentity: optionalString(runtime['contextWindowIdentity']),
    representation: optionalString(runtime['representation']),
  };
}

function occurrenceTruncation(
  metadata: Record<string, unknown>,
): 'observed' | 'not_observed' | 'unknown' {
  const value = metadata['truncation'];
  return value === 'observed' || value === 'not_observed' ? value : 'unknown';
}

function comparePresentation(
  entries: SkillCatalogPresentationEntry[],
  observedAt: string | null,
  expected: ParsedExpectedRealization | null,
  expectedRow: ExpectedRealizationPersistenceRow | undefined,
): SkillExpectedComparison {
  if (!expectedRow) {
    return { status: 'unavailable', reason: 'no_expected_realization' };
  }
  if (!expected) {
    return { status: 'unavailable', reason: 'invalid_expected_realization' };
  }
  if (!observedAt || !Number.isFinite(Date.parse(observedAt))) {
    return { status: 'unavailable', reason: 'occurrence_timestamp_unavailable' };
  }
  const observedEpoch = Date.parse(observedAt);
  if (
    observedEpoch < Date.parse(expected.realization.validFrom)
    || (
      expected.realization.validTo !== null
      && observedEpoch >= Date.parse(expected.realization.validTo)
    )
  ) {
    return {
      status: 'unavailable',
      reason: 'realization_not_valid_for_occurrence',
    };
  }

  const observedByName = new Map<string, Array<string | null>>();
  for (const entry of entries) {
    const values = observedByName.get(entry.name) ?? [];
    values.push(entry.descriptionFingerprint);
    observedByName.set(entry.name, values);
  }
  const expectedNames = new Set(
    expected.realization.skills.map(skill => skill.name),
  );
  const matching: string[] = [];
  const omitted: string[] = [];
  const descriptionMismatched: Array<{
    name: string;
    expectedFingerprint: string;
    observedFingerprints: Array<string | null>;
  }> = [];
  for (const skill of expected.realization.skills) {
    const observedFingerprints = observedByName.get(skill.name);
    if (!observedFingerprints) {
      omitted.push(skill.name);
    } else if (observedFingerprints.includes(skill.descriptionFingerprint)) {
      matching.push(skill.name);
    } else {
      descriptionMismatched.push({
        name: skill.name,
        expectedFingerprint: skill.descriptionFingerprint,
        observedFingerprints: [...new Set(observedFingerprints)].sort((a, b) =>
          (a ?? '').localeCompare(b ?? '')
        ),
      });
    }
  }
  const unexpected = [...observedByName.keys()]
    .filter(name => !expectedNames.has(name))
    .sort();
  matching.sort();
  omitted.sort();
  descriptionMismatched.sort((a, b) => a.name.localeCompare(b.name));
  return {
    status: 'compared',
    realization: expected.reference,
    matching,
    omitted,
    unexpected,
    descriptionMismatched,
  };
}

function catalogBudget(
  session: SessionSkillContextSessionRow,
  observedAt: string | null,
  measurement: SkillCatalogPresentationMeasurement | null,
  runtime: OccurrenceRuntime,
  expected: ParsedExpectedRealization | null,
  expectedRow: ExpectedRealizationPersistenceRow | undefined,
): SkillCatalogBudget {
  if (!measurement) {
    return { status: 'unknown', reason: 'measurement_unavailable' };
  }
  if (!expectedRow) {
    return { status: 'unknown', reason: 'no_authoritative_limit' };
  }
  if (!expected || !observedAt || !Number.isFinite(Date.parse(observedAt))) {
    return { status: 'unknown', reason: 'limit_authority_unrecognized' };
  }
  const policies = expected.realization.policyArtifacts;
  if (policies.length === 0) {
    return { status: 'unknown', reason: 'no_authoritative_limit' };
  }
  const occurrenceEpoch = Date.parse(observedAt);
  const fresh = policies.filter(policy =>
    Date.parse(policy.observedAt) <= occurrenceEpoch
    && occurrenceEpoch < Date.parse(policy.expiresAt)
  );
  if (fresh.length === 0) {
    return { status: 'unknown', reason: 'policy_not_fresh' };
  }
  if (
    !runtime.harnessVersion
    || !runtime.contextWindowIdentity
    || !runtime.representation
  ) {
    return { status: 'unknown', reason: 'limit_authority_unrecognized' };
  }
  const scoped = fresh.filter(policy =>
    policy.harness === session.agent
    && policy.harnessVersion === runtime.harnessVersion
    && policy.contextWindowIdentity === runtime.contextWindowIdentity
    && policy.runtimeRepresentation === runtime.representation
    && (
      policy.model === null
      || (
        runtime.model !== null
        && policy.model === runtime.model
        && (
          policy.modelVersion === null
          || policy.modelVersion === runtime.modelVersion
        )
      )
    )
  );
  if (scoped.length === 0) {
    return { status: 'unknown', reason: 'policy_scope_mismatch' };
  }
  const unitCompatible = scoped.filter(
    policy => policy.limitUnit === measurement.unit,
  );
  if (unitCompatible.length === 0) {
    return { status: 'unknown', reason: 'incompatible_units' };
  }
  const compatible = unitCompatible.filter(
    policy => policy.measurementMethod === measurement.method,
  );
  if (compatible.length === 0) {
    return { status: 'unknown', reason: 'incompatible_measurement_methods' };
  }
  if (compatible.length > 1) {
    return { status: 'unknown', reason: 'limit_authority_ambiguous' };
  }
  const policy = compatible[0]!;
  return {
    status: 'available',
    used: measurement.value,
    limit: policy.limitValue,
    ratio: measurement.value / policy.limitValue,
    unit: measurement.unit,
    measurementMethod: measurement.method,
    policyArtifactId: policy.artifactId,
    policyArtifactRevision: policy.artifactRevision,
    policyArtifactHash: policy.contentHash,
  };
}

function catalogState(
  session: SessionSkillContextSessionRow,
  capabilities: StoredCapabilities,
  observations: SessionSkillContextObservationRow[],
  entryRows: SessionSkillContextCatalogEntryRow[],
  expected: ParsedExpectedRealization | null,
  expectedRow: ExpectedRealizationPersistenceRow | undefined,
): SkillCatalogPresentationState {
  const presentationRows = observations.filter(
    observation => observation.kind === 'catalog_presentation',
  );
  if (
    capabilities.catalogPresentation?.observable !== true
    || presentationRows.length === 0
  ) {
    const reason = capabilities.catalogPresentation?.reason === 'harness_signal_unavailable'
      ? 'harness_signal_unavailable'
      : 'presentation_signal_absent';
    return {
      observable: false,
      reason,
      occurrences: [],
      comparison: {
        status: 'unavailable',
        reason: 'presentation_unobservable',
      },
      budget: {
        status: 'unknown',
        reason: 'measurement_unavailable',
      },
    };
  }

  const entries = catalogEntriesByObservation(entryRows);
  const occurrences: SkillCatalogPresentationOccurrence[] = presentationRows.map(row => {
    const occurrenceEntries = entries.get(row.id) ?? [];
    const metadata = parseRecord(row.metadata_json) ?? {};
    const measurement = catalogMeasurement(metadata);
    const runtime = occurrenceRuntime(metadata);
    return {
      observationId: row.id,
      ordinal: row.ordinal,
      source: row.source,
      observedAt: row.observed_at,
      projectIdentity: row.project_identity,
      fingerprint: catalogFingerprint(occurrenceEntries),
      entries: occurrenceEntries,
      measurement,
      truncation: occurrenceTruncation(metadata),
      runtime,
      comparison: comparePresentation(
        occurrenceEntries,
        row.observed_at,
        expected,
        expectedRow,
      ),
      budget: catalogBudget(
        session,
        row.observed_at,
        measurement,
        runtime,
        expected,
        expectedRow,
      ),
    };
  });
  return { observable: true, occurrences };
}

function instructionOccurrenceFromMetadata(
  ordinal: number,
  source: string,
  observedAt: string | null,
  metadata: Record<string, unknown>,
  reason: string | null = null,
): SkillInstructionLoadOccurrence {
  return {
    ordinal,
    source,
    observedAt,
    filePath: optionalString(metadata['file_path']),
    memoryType: optionalString(metadata['memory_type']),
    loadReason: optionalString(metadata['load_reason']) ?? reason,
    triggerFilePath: optionalString(metadata['trigger_file_path']),
    parentFilePath: optionalString(metadata['parent_file_path']),
  };
}

function claudeInstructionState(
  events: SessionSkillContextInstructionEventRow[],
): SkillInstructionReachState {
  let instrumented = false;
  const occurrences: SkillInstructionLoadOccurrence[] = [];
  for (const event of events) {
    const metadata = parseRecord(event.metadata) ?? {};
    if (
      event.event_type === 'session_start'
      && metadata['instruction_load_instrumented'] === true
    ) {
      instrumented = true;
    }
    if (event.event_type === 'instruction_load') {
      occurrences.push(instructionOccurrenceFromMetadata(
        event.id,
        event.source,
        event.observed_at,
        metadata,
      ));
    }
  }
  if (occurrences.length > 0) return { observable: true, occurrences };
  return instrumented
    ? {
        observable: false,
        reason: 'instrumented_no_events_received',
        occurrences: [],
      }
    : {
        observable: false,
        reason: 'instruction_load_signal_absent',
        occurrences: [],
      };
}

function codexInstructionState(
  capabilities: StoredCapabilities,
  observations: SessionSkillContextObservationRow[],
): SkillInstructionReachState {
  if (capabilities.instructionLoads?.observable !== true) {
    return {
      observable: false,
      reason: capabilities.instructionLoads?.reason === 'harness_signal_unavailable'
        ? 'harness_signal_unavailable'
        : 'instruction_load_signal_absent',
      occurrences: [],
    };
  }
  const occurrences = observations
    .filter(observation => observation.kind === 'instruction_load')
    .map(observation => instructionOccurrenceFromMetadata(
      observation.ordinal,
      observation.source,
      observation.observed_at,
      parseRecord(observation.metadata_json) ?? {},
      observation.reason,
    ));
  return { observable: true, occurrences };
}

function boundedRows<T>(rows: T[], limit: number, label: string): T[] {
  if (rows.length > limit) {
    throw new Error(`Session skill-context ${label} exceeds projection limit ${limit}`);
  }
  return rows;
}

export function getSessionSkillContext(
  db: Database.Database,
  sessionId: string,
): SessionSkillContext | null {
  const session = selectSessionSkillContextSession(db, sessionId);
  if (!session || (session.agent !== 'claude' && session.agent !== 'codex')) {
    return null;
  }
  const observations = boundedRows(
    selectSessionSkillContextObservations(db, sessionId, MAX_OBSERVATIONS + 1),
    MAX_OBSERVATIONS,
    'observations',
  );
  const entries = boundedRows(
    selectSessionSkillContextCatalogEntries(db, sessionId, MAX_CATALOG_ENTRIES + 1),
    MAX_CATALOG_ENTRIES,
    'catalog entries',
  );
  const events = session.agent === 'claude'
    ? boundedRows(
        selectSessionSkillContextInstructionEvents(
          db,
          sessionId,
          MAX_INSTRUCTION_EVENTS + 1,
        ),
        MAX_INSTRUCTION_EVENTS,
        'instruction events',
      )
    : [];
  const capabilities = parseCapabilities(session.skill_context_capabilities_json);
  const classification = classificationCapability(capabilities);
  const consultationClasses = classifyObservations(
    observations,
    classification.observable,
  );
  const expectedRow = selectSessionExpectedRealizationPersistenceRow(db, sessionId);
  const expected = parseExpectedRealization(expectedRow, session.agent);

  return {
    sessionId: session.id,
    harness: session.agent,
    startedAt: session.started_at,
    endedAt: session.ended_at,
    active: session.live_status !== null
      && ACTIVE_SESSION_STATUSES.has(session.live_status),
    consultationClassification: classification.observable
      ? { observable: true }
      : {
          observable: false,
          reason: classification.reason ?? 'missing_ordered_session_projection',
        },
    observations: projectObservations(observations, consultationClasses),
    catalog: catalogState(
      session,
      capabilities,
      observations,
      entries,
      expected,
      expectedRow,
    ),
    expectedRealization: expectedState(expectedRow, expected),
    instructions: session.agent === 'claude'
      ? claudeInstructionState(events)
      : codexInstructionState(capabilities, observations),
  };
}
