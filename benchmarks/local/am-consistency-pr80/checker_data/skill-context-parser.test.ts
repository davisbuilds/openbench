import assert from 'node:assert/strict';
import test from 'node:test';
import { parseSessionMessages } from '../src/parser/claude-code.js';
import { parseCodexSessionMessages } from '../src/parser/codex-sessions.js';

const catalog = `<skills_instructions>
<skills>
<skill><name>test-strategy</name><description>Test behavior.</description><location>/skills/test-strategy/SKILL.md</location><scope>global</scope></skill>
</skills>
</skills_instructions>`;

const runtimeMarkdownCatalog = `<skills_instructions>
## Skills

### Available skills
- test-strategy: Guide agents to test behavior. (file: /skills/test-strategy/SKILL.md)
- github:yeet: Publish local changes to GitHub. (file: /plugins/github/skills/yeet/SKILL.md)
- test-strategy: Project-specific testing guidance. (file: /work/alpha/.agents/skills/test-strategy/SKILL.md)
</skills_instructions>`;

test('Claude preserves consultations and compaction in source order', () => {
  const parsed = parseSessionMessages([
    JSON.stringify({
      type: 'assistant',
      cwd: '/work/alpha',
      timestamp: '2026-07-01T00:00:01Z',
      message: {
        role: 'assistant',
        content: [{ type: 'tool_use', id: 'one', name: 'Skill', input: { skill: 'test-strategy' } }],
      },
    }),
    JSON.stringify({
      type: 'assistant',
      cwd: '/work/alpha',
      timestamp: '2026-07-01T00:00:02Z',
      message: {
        role: 'assistant',
        content: [{ type: 'tool_use', id: 'two', name: 'Skill', input: { skill: 'test-strategy' } }],
      },
    }),
    JSON.stringify({
      type: 'system',
      subtype: 'compact_boundary',
      cwd: '/work/alpha',
      timestamp: '2026-07-01T00:00:03Z',
    }),
    JSON.stringify({
      type: 'assistant',
      cwd: '/work/beta',
      timestamp: '2026-07-01T00:00:04Z',
      message: {
        role: 'assistant',
        content: [{ type: 'tool_use', id: 'three', name: 'Skill', input: { skill: 'test-strategy' } }],
      },
    }),
  ].join('\n'), 'claude-context');

  assert.deepEqual(
    parsed.skillContext?.observations.map(observation => [
      observation.kind,
      observation.skillName ?? null,
      observation.ordinal,
    ]),
    [
      ['consultation', 'test-strategy', 0],
      ['consultation', 'test-strategy', 1],
      ['compaction', null, 2],
      ['consultation', 'test-strategy', 3],
    ],
  );
  assert.equal(parsed.skillContext?.capabilities.orderedConsultations.observable, true);
  assert.notEqual(
    parsed.skillContext?.observations[0]?.projectIdentity,
    parsed.skillContext?.observations[3]?.projectIdentity,
  );
});

test('Codex preserves initial and post-compaction catalog occurrences', () => {
  const parsed = parseCodexSessionMessages([
    JSON.stringify({
      type: 'session_meta',
      timestamp: '2026-07-01T00:00:00Z',
      payload: {
        cwd: '/work/alpha',
        originator: 'codex_cli_rs',
        cli_version: '0.145.0',
      },
    }),
    JSON.stringify({
      type: 'response_item',
      timestamp: '2026-07-01T00:00:01Z',
      payload: {
        role: 'developer',
        content: [{ type: 'input_text', text: catalog }],
      },
    }),
    JSON.stringify({
      type: 'turn_context',
      timestamp: '2026-07-01T00:00:01Z',
      payload: { cwd: '/work/alpha', model: 'gpt-5.6-terra' },
    }),
    JSON.stringify({
      type: 'event_msg',
      timestamp: '2026-07-01T00:00:01Z',
      payload: {
        type: 'token_count',
        info: { model_context_window: 258_400 },
      },
    }),
    JSON.stringify({
      type: 'response_item',
      timestamp: '2026-07-01T00:00:02Z',
      payload: {
        name: 'exec_command',
        arguments: JSON.stringify({ cmd: 'sed -n 1,80p /skills/test-strategy/SKILL.md' }),
      },
    }),
    JSON.stringify({
      type: 'compacted',
      timestamp: '2026-07-01T00:00:03Z',
      payload: {
        replacement_history: [{
          type: 'response_item',
          payload: {
            role: 'developer',
            content: [{
              type: 'input_text',
              text: catalog.replace('Test behavior.', 'Test behavior carefully.'),
            }],
          },
        }],
      },
    }),
    JSON.stringify({
      type: 'response_item',
      timestamp: '2026-07-01T00:00:04Z',
      payload: {
        name: 'exec_command',
        arguments: JSON.stringify({ cmd: 'cat /skills/test-strategy/SKILL.md' }),
      },
    }),
  ].join('\n'), 'codex-context');

  const observations = parsed.skillContext?.observations ?? [];
  assert.equal(observations.filter(item => item.kind === 'consultation').length, 2);
  assert.equal(observations.filter(item => item.kind === 'compaction').length, 1);
  const presentations = observations.filter(item => item.kind === 'catalog_presentation');
  assert.equal(presentations.length, 2);
  assert.notEqual(
    presentations[0]?.metadata?.['fingerprint'],
    presentations[1]?.metadata?.['fingerprint'],
  );
  assert.deepEqual(presentations[0]?.catalogEntries?.map(entry => entry.name), ['test-strategy']);
  assert.equal(
    (presentations[0]?.metadata?.['measurement'] as { unit?: string }).unit,
    'utf8_bytes',
  );
  assert.deepEqual(presentations[0]?.metadata?.['runtime'], {
    harnessVersion: '0.145.0',
    model: 'gpt-5.6-terra',
    modelVersion: null,
    contextWindowIdentity: 'tokens:258400',
    representation: 'skills_instructions_xml',
  });
});

test('Codex parses entries from the runtime Markdown skill catalog', () => {
  const parsed = parseCodexSessionMessages([
    JSON.stringify({
      type: 'session_meta',
      timestamp: '2026-07-01T00:00:00Z',
      payload: { cwd: '/work/alpha', originator: 'codex_cli_rs' },
    }),
    JSON.stringify({
      type: 'response_item',
      timestamp: '2026-07-01T00:00:01Z',
      payload: {
        role: 'developer',
        content: [{ type: 'input_text', text: runtimeMarkdownCatalog }],
      },
    }),
  ].join('\n'), 'codex-markdown-catalog');

  const presentation = parsed.skillContext?.observations.find(
    observation => observation.kind === 'catalog_presentation',
  );
  assert.ok(presentation);
  assert.deepEqual(
    presentation.catalogEntries?.map(entry => ({
      name: entry.name,
      description: entry.description,
      sourceLocation: entry.sourceLocation,
    })),
    [
      {
        name: 'test-strategy',
        description: 'Guide agents to test behavior.',
        sourceLocation: '/skills/test-strategy/SKILL.md',
      },
      {
        name: 'github:yeet',
        description: 'Publish local changes to GitHub.',
        sourceLocation: '/plugins/github/skills/yeet/SKILL.md',
      },
      {
        name: 'test-strategy',
        description: 'Project-specific testing guidance.',
        sourceLocation: '/work/alpha/.agents/skills/test-strategy/SKILL.md',
      },
    ],
  );
});

test('Codex preserves catalog bytes across contiguous content fragments', () => {
  const fragmentedCatalog =
    '<skills_instructions><skills><skill><name>test-strategy</name>'
    + '<description>Test behavior.</description></skill></skills></skills_instructions>';
  const parsed = parseCodexSessionMessages(JSON.stringify({
    type: 'response_item',
    timestamp: '2026-07-01T00:00:01Z',
    payload: {
      role: 'developer',
      content: [
        { type: 'input_text', text: '<skills_instr' },
        {
          type: 'input_text',
          text: 'uctions><skills><skill><name>test-strategy</name>',
        },
        {
          type: 'input_text',
          text: '<description>Test behavior.</description></skill></skills></skills_instructions>',
        },
      ],
    },
  }), 'codex-fragmented-catalog');

  const presentation = parsed.skillContext?.observations.find(
    observation => observation.kind === 'catalog_presentation',
  );
  assert.ok(presentation);
  assert.deepEqual(presentation.catalogEntries?.map(entry => entry.name), ['test-strategy']);
  assert.equal(
    (presentation.metadata?.['measurement'] as { value?: number }).value,
    Buffer.byteLength(fragmentedCatalog),
  );
});

test('Codex does not report an unrecognized catalog body as observed empty', () => {
  const parsed = parseCodexSessionMessages(JSON.stringify({
    type: 'response_item',
    timestamp: '2026-07-01T00:00:01Z',
    payload: {
      role: 'developer',
      content: [{
        type: 'input_text',
        text: '<skills_instructions>opaque future format</skills_instructions>',
      }],
    },
  }), 'codex-unknown-catalog');

  assert.equal(
    parsed.skillContext?.observations.some(
      observation => observation.kind === 'catalog_presentation',
    ),
    false,
  );
  assert.deepEqual(
    parsed.skillContext?.capabilities.catalogPresentation,
    { observable: false, reason: 'presentation_signal_absent' },
  );
});

test('Codex retains explicit AGENTS world-state reach without instruction contents', () => {
  const parsed = parseCodexSessionMessages(JSON.stringify({
    type: 'world_state',
    timestamp: '2026-07-01T00:00:01Z',
    payload: {
      full: true,
      state: {
        agents_md: {
          directory: '/work/project',
          text: 'secret instruction contents',
        },
      },
    },
  }), 'codex-agents-world-state');

  assert.deepEqual(parsed.skillContext?.capabilities.instructionLoads, {
    observable: true,
  });
  const loads = parsed.skillContext?.observations.filter(
    observation => observation.kind === 'instruction_load',
  ) ?? [];
  assert.equal(loads.length, 1);
  assert.equal(loads[0]?.metadata?.['file_path'], '/work/project/AGENTS.md');
  assert.equal(JSON.stringify(loads).includes('secret instruction contents'), false);
});

test('Codex explicit empty AGENTS world state is observed empty', () => {
  const parsed = parseCodexSessionMessages(JSON.stringify({
    type: 'world_state',
    timestamp: '2026-07-01T00:00:01Z',
    payload: {
      full: true,
      state: { agents_md: null },
    },
  }), 'codex-empty-agents-world-state');

  assert.deepEqual(parsed.skillContext?.capabilities.instructionLoads, {
    observable: true,
  });
  assert.equal(
    parsed.skillContext?.observations.some(
      observation => observation.kind === 'instruction_load',
    ),
    false,
  );
});

test('malformed retained source degrades capability without discarding detected consultations', () => {
  const parsed = parseCodexSessionMessages([
    '{"broken":',
    JSON.stringify({
      type: 'response_item',
      timestamp: '2026-07-01T00:00:01Z',
      payload: {
        name: 'exec_command',
        arguments: JSON.stringify({ cmd: 'cat /skills/diagnose/SKILL.md' }),
      },
    }),
  ].join('\n'), 'codex-degraded');

  assert.equal(parsed.skillContext?.observations.length, 1);
  assert.equal(parsed.skillContext?.capabilities.orderedConsultations.observable, false);
  assert.equal(
    parsed.skillContext?.capabilities.orderedConsultations.reason,
    'malformed_source_record',
  );
});
