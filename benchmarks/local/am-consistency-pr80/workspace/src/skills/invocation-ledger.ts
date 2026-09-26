import type Database from 'better-sqlite3';
import type { AnalyticsParams } from '../api/v2/types.js';
import {
  extractCanonicalCodexSessionId,
  extractCodexCommandFromEventMetadata,
  extractCodexCommandFromInputJson,
  extractCodexSkillNamesFromCommand,
  extractExplicitSkillName,
  fingerprintCodexCommand,
} from './invocation-detection.js';

export type SkillInvocationDetectionSource =
  | 'explicit_skill_tool'
  | 'codex_otel'
  | 'codex_jsonl';

export interface SkillInvocationOccurrence {
  skillName: string;
  timestamp: string;
  project: string | null;
  harness: string;
  sessionId: string;
  canonicalSessionId: string;
  messageOrdinal: number | null;
  toolUseId: string | null;
  detectionSource: SkillInvocationDetectionSource;
  commandFingerprint: string | null;
  occurrenceIndex: number;
  matchedObservation: {
    id: number;
    sessionId: string;
    ordinal: number;
    projectIdentity: string | null;
  } | null;
  classificationCapability: {
    observable: boolean;
    reason?: string;
  };
}

function isDateWithinRange(date: string, params: AnalyticsParams): boolean {
  if (params.date_from && date < params.date_from) return false;
  if (params.date_to && date > params.date_to) return false;
  return true;
}

function nextUtcDate(date: string): string | null {
  const parsed = new Date(`${date}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  parsed.setUTCDate(parsed.getUTCDate() + 1);
  return parsed.toISOString().slice(0, 10);
}

function assignOccurrenceIndexes(
  occurrences: Omit<
    SkillInvocationOccurrence,
    'occurrenceIndex' | 'matchedObservation' | 'classificationCapability'
  >[],
): SkillInvocationOccurrence[] {
  const indexes = new Map<string, number>();
  return occurrences.map(occurrence => {
    const key = [
      occurrence.canonicalSessionId,
      occurrence.skillName,
      occurrence.commandFingerprint ?? '',
    ].join('\0');
    const occurrenceIndex = indexes.get(key) ?? 0;
    indexes.set(key, occurrenceIndex + 1);
    return {
      ...occurrence,
      occurrenceIndex,
      matchedObservation: null,
      classificationCapability: {
        observable: false,
        reason: 'missing_ordered_session_projection',
      },
    };
  });
}

interface StoredConsultationObservation {
  id: number;
  session_id: string;
  ordinal: number;
  skill_name: string;
  command_fingerprint: string | null;
  project_identity: string | null;
  skill_context_capabilities_json: string | null;
}

function enrichWithOrderedObservations(
  db: Database.Database,
  occurrences: SkillInvocationOccurrence[],
  params: AnalyticsParams,
): void {
  const skillNames = [...new Set(occurrences.map(occurrence => occurrence.skillName))];
  if (skillNames.length === 0) return;
  const rows: StoredConsultationObservation[] = [];
  for (let offset = 0; offset < skillNames.length; offset += 500) {
    const chunk = skillNames.slice(offset, offset + 500);
    const filters = [
      "observation.kind = 'consultation'",
      `observation.skill_name IN (${chunk.map(() => '?').join(', ')})`,
    ];
    const values: unknown[] = [...chunk];
    if (params.project) {
      filters.push('session.project = ?');
      values.push(params.project);
    }
    if (params.agent) {
      filters.push('session.agent = ?');
      values.push(params.agent);
    }
    if (params.date_from) {
      filters.push('observation.observed_at >= ?');
      values.push(params.date_from);
    }
    const observationToExclusive = params.date_to
      ? nextUtcDate(params.date_to)
      : null;
    if (observationToExclusive) {
      filters.push('observation.observed_at < ?');
      values.push(observationToExclusive);
    }
    rows.push(...db.prepare(`
      SELECT
        observation.id,
        observation.session_id,
        observation.ordinal,
        observation.skill_name,
        observation.command_fingerprint,
        observation.project_identity,
        session.skill_context_capabilities_json
      FROM session_context_observations observation
      JOIN browsing_sessions session ON session.id = observation.session_id
      WHERE ${filters.join(' AND ')}
      ORDER BY observation.session_id, observation.ordinal, observation.id
    `).all(...values) as StoredConsultationObservation[]);
  }

  const indexes = new Map<string, number>();
  const candidates = new Map<string, StoredConsultationObservation[]>();
  for (const row of rows) {
    const canonicalSessionId = extractCanonicalCodexSessionId(row.session_id);
    const baseKey = [
      canonicalSessionId,
      row.skill_name,
      row.command_fingerprint ?? '',
    ].join('\0');
    const index = indexes.get(baseKey) ?? 0;
    indexes.set(baseKey, index + 1);
    const key = `${baseKey}\0${index}`;
    const group = candidates.get(key) ?? [];
    group.push(row);
    candidates.set(key, group);
  }

  for (const occurrence of occurrences) {
    const key = [
      occurrence.canonicalSessionId,
      occurrence.skillName,
      occurrence.commandFingerprint ?? '',
      occurrence.occurrenceIndex,
    ].join('\0');
    const matches = candidates.get(key) ?? [];
    if (matches.length !== 1) continue;
    const match = matches[0]!;
    let observable = true;
    let reason: string | undefined;
    try {
      const capabilities = JSON.parse(
        match.skill_context_capabilities_json ?? '{}',
      ) as { orderedConsultations?: { observable?: boolean; reason?: string };
        compactionVisibility?: { observable?: boolean; reason?: string } };
      if (capabilities.orderedConsultations?.observable !== true) {
        observable = false;
        reason = capabilities.orderedConsultations?.reason
          ?? 'consultation_detection_unavailable';
      } else if (capabilities.compactionVisibility?.observable !== true) {
        observable = false;
        reason = capabilities.compactionVisibility?.reason
          ?? 'compaction_visibility_unavailable';
      }
    } catch {
      observable = false;
      reason = 'missing_ordered_session_projection';
    }
    occurrence.matchedObservation = {
      id: match.id,
      sessionId: match.session_id,
      ordinal: match.ordinal,
      projectIdentity: match.project_identity,
    };
    occurrence.classificationCapability = observable
      ? { observable: true }
      : { observable: false, reason };
  }
}

/**
 * Canonical phase-1 invocation selection.
 *
 * Codex OTEL rows suppress JSONL rows only after the OTEL row passes project
 * and date filters and yields at least one concrete skill path. This matches
 * health's historical behavior and intentionally corrects daily's old
 * out-of-window suppression bug.
 */
export function selectSkillInvocationOccurrences(
  db: Database.Database,
  params: AnalyticsParams = {},
): SkillInvocationOccurrence[] {
  const occurrences: Omit<
    SkillInvocationOccurrence,
    'occurrenceIndex' | 'matchedObservation' | 'classificationCapability'
  >[] = [];

  const explicitFilters = [
    "tc.tool_name = 'Skill'",
    'tc.input_json IS NOT NULL',
  ];
  const explicitValues: unknown[] = [];
  if (params.project) {
    explicitFilters.push('bs.project = ?');
    explicitValues.push(params.project);
  }
  if (params.agent) {
    explicitFilters.push('bs.agent = ?');
    explicitValues.push(params.agent);
  }
  if (params.date_from) {
    explicitFilters.push('COALESCE(m.timestamp, bs.started_at) >= ?');
    explicitValues.push(params.date_from);
  }
  const explicitToExclusive = params.date_to ? nextUtcDate(params.date_to) : null;
  if (explicitToExclusive) {
    explicitFilters.push('COALESCE(m.timestamp, bs.started_at) < ?');
    explicitValues.push(explicitToExclusive);
  }
  const explicitRows = db.prepare(`
    SELECT
      tc.session_id,
      COALESCE(m.timestamp, bs.started_at) AS timestamp,
      bs.project,
      bs.agent,
      m.ordinal,
      tc.tool_use_id,
      tc.input_json
    FROM tool_calls tc
    JOIN browsing_sessions bs ON bs.id = tc.session_id
    LEFT JOIN messages m ON m.id = tc.message_id
    WHERE ${explicitFilters.join(' AND ')}
    ORDER BY timestamp, tc.id
  `).all(...explicitValues) as Array<{
    session_id: string;
    timestamp: string | null;
    project: string | null;
    agent: string;
    ordinal: number | null;
    tool_use_id: string | null;
    input_json: string | null;
  }>;

  for (const row of explicitRows) {
    if (params.project && row.project !== params.project) continue;
    if (params.agent && row.agent !== params.agent) continue;
    if (!row.timestamp || !isDateWithinRange(row.timestamp.slice(0, 10), params)) continue;
    const skillName = extractExplicitSkillName(row.input_json);
    if (!skillName) continue;
    occurrences.push({
      skillName,
      timestamp: row.timestamp,
      project: row.project,
      harness: row.agent,
      sessionId: row.session_id,
      canonicalSessionId: row.session_id,
      messageOrdinal: row.ordinal,
      toolUseId: row.tool_use_id,
      detectionSource: 'explicit_skill_tool',
      commandFingerprint: null,
    });
  }

  if (!params.agent || params.agent === 'codex') {
    const eventFilters = [
      "agent_type = 'codex'",
      "event_type = 'tool_use'",
      "tool_name IN ('exec_command', 'exec')",
      "metadata LIKE '%SKILL.md%'",
    ];
    const eventValues: unknown[] = [];
    if (params.project) {
      eventFilters.push('project = ?');
      eventValues.push(params.project);
    }
    if (params.date_from) {
      eventFilters.push('datetime(COALESCE(client_timestamp, created_at)) >= datetime(?)');
      eventValues.push(params.date_from);
    }
    const eventToExclusive = params.date_to ? nextUtcDate(params.date_to) : null;
    if (eventToExclusive) {
      eventFilters.push('datetime(COALESCE(client_timestamp, created_at)) < datetime(?)');
      eventValues.push(eventToExclusive);
    }
    const eventRows = db.prepare(`
      SELECT
        id,
        session_id,
        project,
        COALESCE(client_timestamp, created_at) AS timestamp,
        metadata
      FROM events
      WHERE ${eventFilters.join(' AND ')}
      ORDER BY timestamp, id
    `).all(...eventValues) as Array<{
      id: number;
      session_id: string;
      project: string | null;
      timestamp: string | null;
      metadata: string | null;
    }>;

    const codexSessionsWithEvents = new Set<string>();
    for (const row of eventRows) {
      if (params.project && row.project !== params.project) continue;
      if (!row.timestamp || !isDateWithinRange(row.timestamp.slice(0, 10), params)) continue;
      const command = extractCodexCommandFromEventMetadata(row.metadata);
      if (!command) continue;
      const skillNames = extractCodexSkillNamesFromCommand(command);
      if (skillNames.length === 0) continue;
      const canonicalSessionId = extractCanonicalCodexSessionId(row.session_id);
      codexSessionsWithEvents.add(canonicalSessionId);
      const commandFingerprint = fingerprintCodexCommand(command);
      for (const skillName of skillNames) {
        occurrences.push({
          skillName,
          timestamp: row.timestamp,
          project: row.project,
          harness: 'codex',
          sessionId: row.session_id,
          canonicalSessionId,
          messageOrdinal: null,
          toolUseId: null,
          detectionSource: 'codex_otel',
          commandFingerprint,
        });
      }
    }

    const jsonlFilters = [
      "bs.agent = 'codex'",
      "bs.integration_mode = 'codex-jsonl'",
      "tc.tool_name IN ('exec_command', 'exec')",
      'tc.input_json IS NOT NULL',
      "tc.input_json LIKE '%SKILL.md%'",
    ];
    const jsonlValues: unknown[] = [];
    if (params.project) {
      jsonlFilters.push('bs.project = ?');
      jsonlValues.push(params.project);
    }
    if (params.date_from) {
      jsonlFilters.push('COALESCE(m.timestamp, bs.started_at) >= ?');
      jsonlValues.push(params.date_from);
    }
    const jsonlToExclusive = params.date_to ? nextUtcDate(params.date_to) : null;
    if (jsonlToExclusive) {
      jsonlFilters.push('COALESCE(m.timestamp, bs.started_at) < ?');
      jsonlValues.push(jsonlToExclusive);
    }
    const jsonlRows = db.prepare(`
      SELECT
        tc.id,
        bs.id AS session_id,
        bs.project,
        COALESCE(m.timestamp, bs.started_at) AS timestamp,
        m.ordinal,
        tc.input_json
      FROM tool_calls tc
      JOIN browsing_sessions bs ON bs.id = tc.session_id
      LEFT JOIN messages m ON m.id = tc.message_id
      WHERE ${jsonlFilters.join(' AND ')}
      ORDER BY timestamp, tc.id
    `).all(...jsonlValues) as Array<{
      id: number;
      session_id: string;
      project: string | null;
      timestamp: string | null;
      ordinal: number | null;
      input_json: string | null;
    }>;

    for (const row of jsonlRows) {
      const canonicalSessionId = extractCanonicalCodexSessionId(row.session_id);
      if (codexSessionsWithEvents.has(canonicalSessionId)) continue;
      if (params.project && row.project !== params.project) continue;
      if (!row.timestamp || !isDateWithinRange(row.timestamp.slice(0, 10), params)) continue;
      const command = extractCodexCommandFromInputJson(row.input_json);
      if (!command) continue;
      const commandFingerprint = fingerprintCodexCommand(command);
      for (const skillName of extractCodexSkillNamesFromCommand(command)) {
        occurrences.push({
          skillName,
          timestamp: row.timestamp,
          project: row.project,
          harness: 'codex',
          sessionId: row.session_id,
          canonicalSessionId,
          messageOrdinal: row.ordinal,
          toolUseId: null,
          detectionSource: 'codex_jsonl',
          commandFingerprint,
        });
      }
    }
  }

  const selected = assignOccurrenceIndexes(occurrences);
  enrichWithOrderedObservations(db, selected, params);
  return selected;
}
