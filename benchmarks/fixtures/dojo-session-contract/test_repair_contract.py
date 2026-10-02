"""Public caller/record compatibility examples; no hidden oracle dependency."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from profiles import budget, rollout_codex


def listing(name=None):
    entries = '' if name is None else f'- {name}: Example. (file: /example/skills/{name}/SKILL.md)\n'
    return '<skills_instructions>\nEach entry includes a name, description, and source locator.\n### Available skills\n' + entries + '</skills_instructions>'


def message(role, text, content_type='input_text'):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
            'content': [{'type': content_type, 'text': text}]}}


def read(*records):
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / 'session.jsonl'
        meta = {'type': 'session_meta', 'payload': {'originator': 'codex-tui'}}
        file.write_text('\n'.join(json.dumps(r) for r in (meta, *records)) + '\n')
        return rollout_codex.read_rollout(file)


class RecordContractTests(unittest.TestCase):
    def test_native_developer_listing_and_conversation_decoys(self):
        for content_type in ('input_text', 'output_text'):
            with self.subTest(content_type=content_type):
                observed = read(message('user', listing('quoted')),
                                message('developer', listing('installed'), content_type),
                                message('assistant', listing('quoted')))
                self.assertIsNotNone(observed)
                self.assertEqual([e.name for e in observed.listing.entries], ['installed'])

    def test_conversation_and_compaction_alone_are_absent(self):
        records = [message(role, listing('quoted')) for role in ('user', 'assistant', 'tool')]
        records.append({'type': 'compacted', 'payload': {'message': listing('quoted')}})
        self.assertIsNone(read(*records))

    def test_empty_native_listing_is_observed(self):
        observed = read(message('developer', listing()))
        self.assertIsNotNone(observed)
        self.assertEqual(list(observed.listing.entries), [])

    def test_legacy_fixture_remains_supported(self):
        for record_type in ('turn_context', 'turn'):
            with self.subTest(record_type=record_type):
                observed = read({'type': record_type, 'payload': {'text': listing('legacy')}})
                self.assertIsNotNone(observed)
                self.assertEqual([e.name for e in observed.listing.entries], ['legacy'])

    def test_native_listing_precedes_legacy_fixture(self):
        observed = read({'type': 'turn_context', 'payload': {'text': listing('legacy')}},
                        message('developer', listing('native')))
        self.assertIsNotNone(observed)
        self.assertEqual([e.name for e in observed.listing.entries], ['native'])


class BudgetContractTests(unittest.TestCase):
    def test_mode_reaches_both_public_entry_points(self):
        policy = budget.Policy(harness='codex', harness_version='example', model='example',
            unit='tokens', limit=4000, context_window=None, window_field=None,
            estimator='example', provenance='synthetic', measured='example', probe='example',
            deployable=True, shadows_by_name=False, project_scope_root='.agents/skills',
            limit_basis='observed', declared_surfaces=('codex-tui',))
        entries = [{'name': 'installed', 'source_description': 'Example.',
                    'listed_description': 'Example.', 'locator': '/example/skills/installed/SKILL.md'}]
        for surface in ('codex-tui', 'exec', None):
            with self.subTest(surface=surface):
                assessment = budget.assess(entries, policy, surface=surface)
                direct = budget.Assessment(policy, 4001, 4000, 'tokens',
                                           budget.Verdict.NONCONFORMANT, surface=surface)
                self.assertEqual(assessment.gating, surface == 'codex-tui')
                self.assertEqual(direct.gating, surface == 'codex-tui')
                self.assertEqual(assessment.verdict, budget.Verdict.DEPLOYABLE if surface == 'codex-tui'
                                 else budget.Verdict.UNSUPPORTED)


if __name__ == '__main__':
    unittest.main()
