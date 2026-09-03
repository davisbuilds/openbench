import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { after, before, test } from 'node:test';
import type { getDb as getDbType, closeDb as closeDbType } from '../src/db/connection.js';
import type { getAnalyticsSkillConsultations as getAnalyticsSkillConsultationsType } from '../src/db/v2-queries.js';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'amon-consultations-'));
process.env['AGENTMONITOR_DB_PATH'] = path.join(tempDir, 'test.db');
process.env['AGENTMONITOR_SKILL_DIRS'] = path.join(tempDir, 'skills');

let getDb: typeof getDbType;
let closeDb: typeof closeDbType;
let getAnalyticsSkillConsultations: typeof getAnalyticsSkillConsultationsType;

before(async () => {
  ({ getDb, closeDb } = await import('../src/db/connection.js'));
  const { initSchema } = await import('../src/db/schema.js');
  const { insertParsedSession, parseSessionMessages } = await import('../src/parser/claude-code.js');
  const { parseCodexSessionMessages } = await import('../src/parser/codex-sessions.js');
  ({ getAnalyticsSkillConsultations } = await import('../src/db/v2-queries.js'));
  initSchema();
  getDb().prepare(`
    INSERT INTO skill_catalog_snapshots (name, version, first_seen_at, last_seen_at)
    VALUES ('test-strategy', '2.0.0', '2026-06-01T00:00:00Z', '2026-08-01T00:00:00Z')
  `).run();

  const line = (timestamp: string, id: string, cwd = '/work/alpha') => JSON.stringify({
    type: 'assistant',
    cwd,
    timestamp,
    message: {
      role: 'assistant',
      content: [{ type: 'tool_use', id, name: 'Skill', input: { skill: 'test-strategy' } }],
    },
  });
  const fullPath = '/tmp/.claude/projects/-Users-test-Dev-alpha/full.jsonl';
  const full = parseSessionMessages([
    line('2026-07-10T10:00:00Z', 'one'),
    line('2026-07-10T10:01:00Z', 'two'),
    JSON.stringify({
      type: 'system',
      subtype: 'compact_boundary',
      cwd: '/work/alpha',
      timestamp: '2026-07-10T10:02:00Z',
    }),
    line('2026-07-10T10:03:00Z', 'three', '/work/beta'),
  ].join('\n'), 'eligible-session', fullPath);
  insertParsedSession(
    getDb(),
    full,
    fullPath,
    10,
    'full',
  );

  const degradedPath = '/tmp/.claude/projects/-Users-test-Dev-alpha/degraded.jsonl';
  const degraded = parseSessionMessages([
    '{"broken":',
    line('2026-07-10T11:00:00Z', 'degraded'),
  ].join('\n'), 'degraded-session', degradedPath);
  insertParsedSession(
    getDb(),
    degraded,
    degradedPath,
    10,
    'degraded',
  );

  const windowHistoryPath =
    '/tmp/.claude/projects/-Users-test-Dev-window-history/session.jsonl';
  const windowHistory = parseSessionMessages([
    line('2026-07-09T23:58:00Z', 'window-history-first', '/work/window-history'),
    JSON.stringify({
      type: 'system',
      subtype: 'compact_boundary',
      cwd: '/work/window-history',
      timestamp: '2026-07-09T23:59:00Z',
    }),
    line('2026-07-10T00:01:00Z', 'window-history-second', '/work/window-history'),
  ].join('\n'), 'window-history-session', windowHistoryPath);
  insertParsedSession(
    getDb(),
    windowHistory,
    windowHistoryPath,
    10,
    'window-history',
  );

  const activeAcrossWindowPath =
    '/tmp/.claude/projects/-Users-test-Dev-active-window/across.jsonl';
  const activeAcrossWindow = parseSessionMessages([
    JSON.stringify({
      type: 'user',
      cwd: '/work/active-window',
      timestamp: '2026-07-09T23:00:00Z',
      message: {
        role: 'user',
        content: [{ type: 'text', text: 'Keep working.' }],
      },
    }),
  ].join('\n'), 'active-across-window', activeAcrossWindowPath);
  insertParsedSession(
    getDb(),
    activeAcrossWindow,
    activeAcrossWindowPath,
    10,
    'active-across-window',
  );
  getDb().prepare(`
    UPDATE browsing_sessions
    SET live_status = 'live'
    WHERE id = 'active-across-window'
  `).run();

  const activeWindowInvocationPath =
    '/tmp/.claude/projects/-Users-test-Dev-active-window/invocation.jsonl';
  const activeWindowInvocation = parseSessionMessages([
    line('2026-07-10T10:00:00Z', 'active-window-read', '/work/active-window'),
  ].join('\n'), 'active-window-invocation', activeWindowInvocationPath);
  insertParsedSession(
    getDb(),
    activeWindowInvocation,
    activeWindowInvocationPath,
    10,
    'active-window-invocation',
  );

  const codexUuid = '019d1234-1234-7234-8234-123456789abc';
  const codexSessionId = `rollout-2026-07-10T12-00-00-${codexUuid}`;
  const runtimeCatalog = `<skills_instructions>
## Skills
- test-strategy: Guide agents to test behavior. (file: /skills/test-strategy/SKILL.md)
</skills_instructions>`;
  const codexTool = (timestamp: string) => JSON.stringify({
    type: 'response_item',
    timestamp,
    payload: {
      name: 'exec_command',
      arguments: JSON.stringify({ cmd: 'cat /skills/test-strategy/SKILL.md' }),
    },
  });
  const codex = parseCodexSessionMessages([
    JSON.stringify({
      type: 'session_meta',
      timestamp: '2026-07-10T12:00:00Z',
      payload: { cwd: '/work/codex-project', originator: 'codex_cli_rs' },
    }),
    JSON.stringify({
      type: 'response_item',
      timestamp: '2026-07-10T12:00:01Z',
      payload: {
        role: 'developer',
        content: [{ type: 'input_text', text: runtimeCatalog }],
      },
    }),
    codexTool('2026-07-10T12:00:02Z'),
    JSON.stringify({
      type: 'compacted',
      timestamp: '2026-07-10T12:00:03Z',
      payload: { replacement_history: [] },
    }),
    codexTool('2026-07-10T12:00:04Z'),
  ].join('\n'), codexSessionId);
  insertParsedSession(getDb(), codex, '/tmp/codex.jsonl', 10, 'codex');

  const insertEvent = getDb().prepare(`
    INSERT INTO events (
      event_id, session_id, agent_type, event_type, tool_name, status,
      project, created_at, client_timestamp, metadata, source
    ) VALUES (?, ?, 'codex', 'tool_use', 'exec', 'success', ?, ?, ?, ?, 'otel')
  `);
  for (const [index, timestamp] of [
    '2026-07-10T12:00:02Z',
    '2026-07-10T12:00:04Z',
  ].entries()) {
    insertEvent.run(
      `codex-canonical-${index}`,
      codexUuid,
      'codex-project',
      timestamp,
      timestamp,
      JSON.stringify({
        arguments: { cmd: 'cat /skills/test-strategy/SKILL.md' },
      }),
    );
  }
});

after(() => {
  closeDb();
  fs.rmSync(tempDir, { recursive: true, force: true });
});

test('classifies consultations and reconciles coverage, projects, and versions', () => {
  const result = getAnalyticsSkillConsultations({
    agent: 'claude',
    project: 'alpha',
    date_from: '2026-07-10',
    date_to: '2026-07-10',
  });
  const harness = result.byHarness.find(item => item.harness === 'claude');
  const row = harness?.skills.find(skill => skill.name === 'test-strategy');
  assert.ok(row);
  assert.deepEqual(row.classes, {
    first_read: 1,
    rehydration_after_compaction: 1,
    repeat_no_compaction: 1,
    unclassifiable: 1,
  });
  assert.equal(row.invocations, 4);
  assert.equal(row.sessionsInWindow, 2);
  assert.equal(row.eligibleSessionsInWindow, 1);
  assert.equal(row.sessionsWithFirstRead, 1);
  assert.equal(row.firstReadEngagementRate, 1);
  assert.equal(
    row.ineligibleSessionsByReason.find(reason => reason.reason === 'malformed_source_record')?.sessions,
    1,
  );
  assert.equal(
    row.projectBreadth.sessions.reduce((sum, project) => sum + project.sessions, 0),
    row.sessionsWithFirstRead,
  );
  assert.equal(row.versions[0]?.version, '2.0.0');
  assert.equal(row.versions[0]?.invocations, row.invocations);
  assert.equal(
    Object.values(row.classes).reduce((sum, count) => sum + count, 0),
    row.invocations,
  );
  assert.equal(result.comparability.status, 'single_harness');
  assert.equal(result.windowSemantics.toExclusive, '2026-07-11T00:00:00.000Z');
});

test('classifies OTEL-backed Codex reads against canonical JSONL compactions and exposure', () => {
  const result = getAnalyticsSkillConsultations({
    agent: 'codex',
    date_from: '2026-07-10',
    date_to: '2026-07-10',
  });
  const row = result.byHarness[0]?.skills.find(skill => skill.name === 'test-strategy');
  assert.ok(row);
  assert.deepEqual(row.classes, {
    first_read: 1,
    rehydration_after_compaction: 1,
    repeat_no_compaction: 0,
    unclassifiable: 0,
  });
  assert.deepEqual(row.exposure, {
    jointlyEligiblePresentedSessions: 1,
    presentedWithFirstRead: 1,
    presentedWithoutFirstRead: 0,
  });
});

test('classifies in-window consultations against earlier history in the same session', () => {
  const result = getAnalyticsSkillConsultations({
    agent: 'claude',
    project: 'window-history',
    date_from: '2026-07-10',
    date_to: '2026-07-10',
  });
  const row = result.byHarness[0]?.skills.find(skill => skill.name === 'test-strategy');
  assert.ok(row);
  assert.deepEqual(row.classes, {
    first_read: 0,
    rehydration_after_compaction: 1,
    repeat_no_compaction: 0,
    unclassifiable: 0,
  });
});

test('counts an active session through the response time despite a stale parsed end', () => {
  const result = getAnalyticsSkillConsultations({
    agent: 'claude',
    project: 'active-window',
    date_from: '2026-07-10',
    date_to: '2026-07-10',
  }, new Date('2026-07-10T12:00:00Z'));
  const row = result.byHarness[0]?.skills.find(skill => skill.name === 'test-strategy');
  assert.ok(row);
  assert.equal(row.sessionsInWindow, 2);
  assert.equal(row.eligibleSessionsInWindow, 2);
  assert.equal(row.firstReadEngagementRate, 0.5);
});
