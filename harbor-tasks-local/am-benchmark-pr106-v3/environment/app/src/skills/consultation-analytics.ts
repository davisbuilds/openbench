import type Database from 'better-sqlite3';
import type { AnalyticsParams, SkillConsultationAnalytics, SkillConsultationClass, SkillConsultationClassCounts, SkillConsultationRow } from '../api/v2/types.js';
import { resolveVersionAt, type CatalogSnapshot } from './catalog.js';
import { extractCanonicalCodexSessionId } from './invocation-detection.js';
import { selectSkillInvocationOccurrences, type SkillInvocationOccurrence } from './invocation-ledger.js';

interface SessionRow {
  id: string;
  agent: string;
  project: string | null;
  project_identity: string | null;
  started_at: string | null;
  ended_at: string | null;
  last_item_at: string | null;
  live_status: string | null;
  skill_context_capabilities_json: string | null;
}

interface StoredObservation {
  id: number;
  session_id: string;
  ordinal: number;
  kind: string;
  skill_name: string | null;
}

const emptyClasses = (): SkillConsultationClassCounts => ({
  first_read: 0,
  rehydration_after_compaction: 0,
  repeat_no_compaction: 0,
  unclassifiable: 0,
});

function utcBoundary(date: string | undefined, addDay: boolean): string | null {
  if (!date) return null;
  const parsed = new Date(`${date}T00:00:00.000Z`);
  if (Number.isNaN(parsed.getTime())) return null;
  if (addDay) parsed.setUTCDate(parsed.getUTCDate() + 1);
  return parsed.toISOString();
}

function capability(
  session: SessionRow,
): { observable: boolean; reason?: string } {
  if (!session.skill_context_capabilities_json) {
    return { observable: false, reason: 'missing_ordered_session_projection' };
  }
  try {
    const parsed = JSON.parse(session.skill_context_capabilities_json) as {
      orderedConsultations?: { observable?: boolean; reason?: string };
      compactionVisibility?: { observable?: boolean; reason?: string };
    };
    if (parsed.orderedConsultations?.observable !== true) {
      return {
        observable: false,
        reason: parsed.orderedConsultations?.reason ?? 'consultation_detection_unavailable',
      };
    }
    if (parsed.compactionVisibility?.observable !== true) {
      return {
        observable: false,
        reason: parsed.compactionVisibility?.reason ?? 'compaction_visibility_unavailable',
      };
    }
    return { observable: true };
  } catch {
    return { observable: false, reason: 'missing_ordered_session_projection' };
  }
}

function classifyOccurrences(
  occurrences: SkillInvocationOccurrence[],
  observations: StoredObservation[],
): Map<SkillInvocationOccurrence, SkillConsultationClass> {
  const compactions = new Map<string, number[]>();
  const consultations = new Map<string, number[]>();
  for (const observation of observations) {
    const sessionId = extractCanonicalCodexSessionId(observation.session_id);
    if (observation.kind === 'compaction') {
      const values = compactions.get(sessionId) ?? [];
      values.push(observation.ordinal);
      compactions.set(sessionId, values);
    } else if (observation.kind === 'consultation' && observation.skill_name) {
      const key = `${sessionId}\0${observation.skill_name}`;
      const values = consultations.get(key) ?? [];
      values.push(observation.ordinal);
      consultations.set(key, values);
    }
  }
  const countBefore = (ordinals: number[], target: number): number => {
    let low = 0;
    let high = ordinals.length;
    while (low < high) {
      const midpoint = Math.floor((low + high) / 2);
      if (ordinals[midpoint]! < target) low = midpoint + 1;
      else high = midpoint;
    }
    return low;
  };
  const classes = new Map<SkillInvocationOccurrence, SkillConsultationClass>();
  for (const occurrence of occurrences) {
    if (!occurrence.matchedObservation || !occurrence.classificationCapability.observable) {
      classes.set(occurrence, 'unclassifiable');
      continue;
    }
    const key = `${occurrence.canonicalSessionId}\0${occurrence.skillName}`;
    const consultationOrdinals = consultations.get(key) ?? [];
    const currentOrdinal = occurrence.matchedObservation.ordinal;
    const priorIndex = countBefore(consultationOrdinals, currentOrdinal) - 1;
    if (priorIndex < 0) {
      classes.set(occurrence, 'first_read');
      continue;
    }
    const compactionOrdinals = compactions.get(occurrence.canonicalSessionId) ?? [];
    const currentGeneration = countBefore(compactionOrdinals, currentOrdinal);
    const previousGeneration = countBefore(
      compactionOrdinals,
      consultationOrdinals[priorIndex]!,
    );
    if (currentGeneration > previousGeneration) {
      classes.set(occurrence, 'rehydration_after_compaction');
    } else {
      classes.set(occurrence, 'repeat_no_compaction');
    }
  }
  return classes;
}

interface SqlFilter {
  clauses: string[];
  values: unknown[];
}

function sessionFilter(params: AnalyticsParams): SqlFilter {
  const clauses = ["agent IN ('claude', 'codex')"];
  const values: unknown[] = [];
  if (params.agent) {
    clauses.push('agent = ?');
    values.push(params.agent);
  }
  if (params.project) {
    clauses.push('project = ?');
    values.push(params.project);
  }
  return { clauses, values };
}

const SESSION_SELECT = `
  SELECT id, agent, project, project_identity, started_at, ended_at,
         last_item_at, live_status, skill_context_capabilities_json
  FROM browsing_sessions
`;

function selectSessions(
  db: Database.Database,
  filter: SqlFilter,
  extraClause?: string,
  extraValues: unknown[] = [],
): SessionRow[] {
  const clauses = extraClause ? [...filter.clauses, extraClause] : filter.clauses;
  return db.prepare(`
    ${SESSION_SELECT}
    WHERE ${clauses.join(' AND ')}
  `).all(...filter.values, ...extraValues) as SessionRow[];
}

function selectSessionsByIds(
  db: Database.Database,
  filter: SqlFilter,
  sessionIds: string[],
): SessionRow[] {
  const rows: SessionRow[] = [];
  for (let offset = 0; offset < sessionIds.length; offset += 500) {
    const chunk = sessionIds.slice(offset, offset + 500);
    const placeholders = chunk.map(() => '?').join(', ');
    rows.push(...selectSessions(
      db,
      filter,
      `id IN (${placeholders})`,
      chunk,
    ));
  }
  return rows;
}

function selectScopedSessions(
  db: Database.Database,
  params: AnalyticsParams,
  occurrences: SkillInvocationOccurrence[],
  asOf: string,
  from: string | null,
  toExclusive: string | null,
): { sessions: SessionRow[]; windowMembershipUnobservable: number } {
  const filter = sessionFilter(params);
  const bounded = Boolean(from || toExclusive);
  if (!bounded) {
    return {
      sessions: selectSessions(db, filter),
      windowMembershipUnobservable: 0,
    };
  }

  const intervalClauses = ['started_at IS NOT NULL'];
  const intervalValues: unknown[] = [];
  if (toExclusive) {
    intervalClauses.push('started_at < ?');
    intervalValues.push(toExclusive.slice(0, 10));
  }
  if (from) {
    intervalClauses.push(`
      CASE
        WHEN live_status IN ('live', 'active', 'available') THEN ?
        ELSE COALESCE(ended_at, last_item_at, ?)
      END >= ?
    `);
    intervalValues.push(asOf, asOf, from.slice(0, 10));
  }
  const byInterval = selectSessions(
    db,
    filter,
    `(${intervalClauses.join(' AND ')})`,
    intervalValues,
  );
  const occurrenceSessionIds = [...new Set(occurrences.flatMap(occurrence => [
    occurrence.sessionId,
    occurrence.matchedObservation?.sessionId,
  ]).filter((id): id is string => Boolean(id)))];
  const byOccurrence = selectSessionsByIds(db, filter, occurrenceSessionIds);
  const sessionsById = new Map(
    [...byInterval, ...byOccurrence].map(session => [session.id, session]),
  );

  const unknownCount = db.prepare(`
    SELECT COUNT(*) AS count
    FROM browsing_sessions
    WHERE ${[...filter.clauses, 'started_at IS NULL'].join(' AND ')}
  `).get(...filter.values) as { count: number };
  const ownedUnknown = new Set(
    byOccurrence
      .filter(session => session.started_at === null)
      .map(session => session.id),
  ).size;
  return {
    sessions: [...sessionsById.values()],
    windowMembershipUnobservable: Math.max(0, unknownCount.count - ownedUnknown),
  };
}

function selectObservations(
  db: Database.Database,
  sessionIds: string[],
): StoredObservation[] {
  const rows: StoredObservation[] = [];
  for (let offset = 0; offset < sessionIds.length; offset += 500) {
    const chunk = sessionIds.slice(offset, offset + 500);
    const placeholders = chunk.map(() => '?').join(', ');
    rows.push(...db.prepare(`
      SELECT id, session_id, ordinal, kind, skill_name
      FROM session_context_observations
      WHERE session_id IN (${placeholders})
      ORDER BY session_id, ordinal, id
    `).all(...chunk) as StoredObservation[]);
  }
  return rows;
}

function selectPresentationRows(
  db: Database.Database,
  sessionIds: string[],
): Array<{ session_id: string; skill_name: string }> {
  const rows: Array<{ session_id: string; skill_name: string }> = [];
  for (let offset = 0; offset < sessionIds.length; offset += 500) {
    const chunk = sessionIds.slice(offset, offset + 500);
    const placeholders = chunk.map(() => '?').join(', ');
    rows.push(...db.prepare(`
      SELECT observation.session_id, entry.skill_name
      FROM session_context_observations observation
      JOIN session_catalog_observation_entries entry
        ON entry.observation_id = observation.id
      WHERE observation.kind = 'catalog_presentation'
        AND observation.session_id IN (${placeholders})
      GROUP BY observation.session_id, entry.skill_name
    `).all(...chunk) as Array<{ session_id: string; skill_name: string }>);
  }
  return rows;
}

interface MutableAggregate {
  row: SkillConsultationRow;
  firstReadSessions: Set<string>;
  projects: Map<string, { label: string; sessions: Set<string> }>;
  versions: Map<string, {
    version: string | null;
    attribution: 'exact' | 'approximate' | 'unknown';
    invocations: number;
    classes: SkillConsultationClassCounts;
  }>;
}

export interface SkillConsultationAnalyticsOptions {
  now?: Date;
  occurrences?: SkillInvocationOccurrence[];
}

export function getSkillConsultationAnalytics(
  db: Database.Database,
  params: AnalyticsParams,
  snapshots: CatalogSnapshot[],
  options: SkillConsultationAnalyticsOptions = {},
): SkillConsultationAnalytics {
  const now = options.now ?? new Date();
  const asOf = now.toISOString();
  const from = utcBoundary(params.date_from, false);
  const toExclusive = utcBoundary(params.date_to, true);
  const occurrences = options.occurrences ?? selectSkillInvocationOccurrences(db, params);
  const scoped = selectScopedSessions(
    db,
    params,
    occurrences,
    asOf,
    from,
    toExclusive,
  );
  const sessions = scoped.sessions;
  const sessionIds = sessions.map(session => session.id);
  const observations = selectObservations(db, sessionIds);
  const classes = classifyOccurrences(occurrences, observations);
  const sessionsByCanonicalId = new Map(
    sessions.map(session => [extractCanonicalCodexSessionId(session.id), session]),
  );
  const sessionsById = new Map(sessions.map(session => [session.id, session]));
  const sessionsByHarness = new Map<string, SessionRow[]>();
  for (const session of sessions) {
    const list = sessionsByHarness.get(session.agent) ?? [];
    list.push(session);
    sessionsByHarness.set(session.agent, list);
  }

  const aggregates = new Map<string, MutableAggregate>();
  const aggregateFor = (harness: string, name: string): MutableAggregate => {
    const key = `${harness}\0${name}`;
    const existing = aggregates.get(key);
    if (existing) return existing;
    const harnessSessions = sessionsByHarness.get(harness) ?? [];
    const eligible = harnessSessions.filter(session => capability(session).observable);
    const reasons = new Map<string, number>();
    for (const session of harnessSessions) {
      const state = capability(session);
      if (!state.observable) {
        const reason = state.reason ?? 'missing_ordered_session_projection';
        reasons.set(reason, (reasons.get(reason) ?? 0) + 1);
      }
    }
    const created: MutableAggregate = {
      row: {
        name,
        harness,
        invocations: 0,
        classes: emptyClasses(),
        sessionsInWindow: harnessSessions.length,
        eligibleSessionsInWindow: eligible.length,
        sessionsWithFirstRead: 0,
        firstReadEngagementRate: null,
        ineligibleSessionsByReason: [...reasons].map(([reason, count]) => ({ reason, sessions: count })),
        projectBreadth: { distinctObservedProjects: 0, sessions: [] },
        versions: [],
        exposure: {
          jointlyEligiblePresentedSessions: 0,
          presentedWithFirstRead: 0,
          presentedWithoutFirstRead: 0,
        },
      },
      firstReadSessions: new Set(),
      projects: new Map(),
      versions: new Map(),
    };
    aggregates.set(key, created);
    return created;
  };

  for (const occurrence of occurrences) {
    const aggregate = aggregateFor(occurrence.harness, occurrence.skillName);
    const classification = classes.get(occurrence) ?? 'unclassifiable';
    aggregate.row.invocations++;
    aggregate.row.classes[classification]++;
    if (classification === 'first_read') {
      aggregate.firstReadSessions.add(occurrence.canonicalSessionId);
      const identity = occurrence.matchedObservation?.projectIdentity ?? 'unknown';
      const session = sessionsByCanonicalId.get(occurrence.canonicalSessionId);
      const label = identity === 'unknown' ? 'Unknown' : (session?.project ?? identity);
      const bucket = aggregate.projects.get(identity) ?? { label, sessions: new Set<string>() };
      bucket.sessions.add(occurrence.canonicalSessionId);
      aggregate.projects.set(identity, bucket);
    }
    const resolved = resolveVersionAt(snapshots, occurrence.skillName, occurrence.timestamp);
    const attribution = resolved.version === null
      ? 'unknown'
      : resolved.approximate ? 'approximate' : 'exact';
    const versionKey = `${resolved.version ?? ''}\0${attribution}`;
    const version = aggregate.versions.get(versionKey) ?? {
      version: resolved.version,
      attribution,
      invocations: 0,
      classes: emptyClasses(),
    };
    version.invocations++;
    version.classes[classification]++;
    aggregate.versions.set(versionKey, version);
  }

  const presentationRows = selectPresentationRows(db, sessionIds);
  const presented = new Map<string, Set<string>>();
  for (const row of presentationRows) {
    const canonicalSessionId = extractCanonicalCodexSessionId(row.session_id);
    const set = presented.get(row.skill_name) ?? new Set<string>();
    set.add(canonicalSessionId);
    presented.set(row.skill_name, set);
    const session = sessionsById.get(row.session_id);
    if (session) aggregateFor(session.agent, row.skill_name);
  }

  for (const aggregate of aggregates.values()) {
    aggregate.row.sessionsWithFirstRead = aggregate.firstReadSessions.size;
    aggregate.row.firstReadEngagementRate = aggregate.row.eligibleSessionsInWindow > 0
      ? aggregate.firstReadSessions.size / aggregate.row.eligibleSessionsInWindow
      : null;
    aggregate.row.projectBreadth.sessions = [...aggregate.projects.entries()]
      .map(([id, value]) => ({ id, label: value.label, sessions: value.sessions.size }))
      .sort((left, right) => right.sessions - left.sessions || left.label.localeCompare(right.label));
    aggregate.row.projectBreadth.distinctObservedProjects = aggregate.row.projectBreadth.sessions
      .filter(project => project.id !== 'unknown').length;
    aggregate.row.versions = [...aggregate.versions.values()];
    const eligibleIds = new Set(
      (sessionsByHarness.get(aggregate.row.harness) ?? [])
        .filter(session => capability(session).observable)
        .map(session => extractCanonicalCodexSessionId(session.id)),
    );
    const presentedIds = [...(presented.get(aggregate.row.name) ?? [])]
      .filter(sessionId => eligibleIds.has(sessionId));
    aggregate.row.exposure.jointlyEligiblePresentedSessions = presentedIds.length;
    aggregate.row.exposure.presentedWithFirstRead = presentedIds
      .filter(sessionId => aggregate.firstReadSessions.has(sessionId)).length;
    aggregate.row.exposure.presentedWithoutFirstRead =
      presentedIds.length - aggregate.row.exposure.presentedWithFirstRead;
  }

  const harnesses = [...new Set([
    ...sessionsByHarness.keys(),
    ...occurrences.map(occurrence => occurrence.harness),
  ])].sort();
  return {
    asOf,
    windowSemantics: {
      interval: 'utc_half_open',
      from,
      toExclusive,
      sessionMembership: 'observed_interval_overlap_or_in_window_occurrence',
      windowMembershipUnobservable: scoped.windowMembershipUnobservable,
    },
    byHarness: harnesses.map(harness => ({
      harness,
      detectionSemantics: harness === 'claude'
        ? 'explicit_skill_tool'
        : 'concrete_skill_path',
      skills: [...aggregates.values()]
        .filter(aggregate => aggregate.row.harness === harness)
        .map(aggregate => aggregate.row)
        .sort((left, right) => right.invocations - left.invocations || left.name.localeCompare(right.name)),
    })),
    comparability: harnesses.length <= 1
      ? { status: 'single_harness', limitingEvidence: [] }
      : {
          status: 'not_directly_comparable',
          limitingEvidence: ['different_detection_semantics'],
        },
  };
}
