"""Offline Claude launch contract exercised by a real disposable CLI process."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from obench.adapters import claude


class ClaudeCapturedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'captured-config'
        self.config.mkdir()
        (self.config / 'settings.json').write_text('{"language":"English"}')
        self.home = self.root / 'captured-home'
        (self.home / '.claude' / 'skills' / 'canary').mkdir(parents=True)
        (self.home / '.claude' / 'skills' / 'canary' / 'SKILL.md').write_text('captured context')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        exe = self.bin / 'claude'
        exe.write_text('#!' + sys.executable + '\n' + r'''
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ['HOME'])
config = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
pathlib.Path('observed.json').write_text(json.dumps({
    'env': dict(os.environ), 'argv': sys.argv[1:],
    'settings': (config / 'settings.json').read_text() if (config / 'settings.json').exists() else None,
    'skill': (home / '.claude/skills/canary/SKILL.md').read_text() if (home / '.claude/skills/canary/SKILL.md').exists() else None,
}))
events = [
    {'type': 'system', 'subtype': 'init'},
    {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'read-1', 'name': 'Read', 'input': {'file_path': 'SKILL.md'}}]}},
    {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'read-1', 'content': 'captured context'}]}},
    {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'finished'}]}},
]
result = {'type': 'result', 'is_error': False, 'result': 'finished', 'num_turns': 2, 'usage': {'input_tokens': 3, 'output_tokens': 4}}
mode = os.environ.get('FIXTURE_MODE')
if 'stream-json' in sys.argv:
    for event in events:
        print(json.dumps(event), flush=True)
    if mode == 'timeout':
        time.sleep(5)
    elif mode == 'malformed':
        print('{broken event', flush=True)
    if mode != 'missing_result':
        print(json.dumps(result), flush=True)
    print(json.dumps({'type': 'system', 'subtype': 'cleanup'}), flush=True)
else:
    print(json.dumps(result), flush=True)
''')
        exe.chmod(0o755)
        self.explicit = {
            'PATH': str(self.bin),
            'CLAUDE_CONFIG_DIR': str(self.config),
            'OPENBENCH_CAPTURED_HOME': str(self.home),
            'OPENBENCH_CLAUDE_AUTH_MODE': 'subscription',
            'CLAUDE_CODE_OAUTH_TOKEN': 'lane-test-token',
        }
        self.ambient = {
            'PATH': str(self.bin), 'HOME': str(self.root / 'daily-home'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'daily-config'),
            'CLAUDE_CODE_OAUTH_TOKEN': 'daily-test-token',
            'ANTHROPIC_API_KEY': 'daily-test-key',
            'ANTHROPIC_AUTH_TOKEN': 'daily-auth',
            'ANTHROPIC_BASE_URL': 'https://invalid.example',
            'CLAUDE_CODE_USE_BEDROCK': '1', 'AWS_ACCESS_KEY_ID': 'daily-aws',
            'ZAI_API_KEY': 'daily-vendor', 'OPENBENCH_PROXY': '1',
            'NODE_OPTIONS': 'daily-injection', 'AMBIENT_SENTINEL': 'must-not-inherit',
        }

    def run_lane(self, explicit=None, replace_env=True, model='claude-opus-4-8', timeout=3):
        with patch.dict(os.environ, self.ambient, clear=True):
            before = dict(os.environ)
            result = claude.run('test', str(self.root), model, timeout,
                                env_override=self.explicit if explicit is None else explicit,
                                replace_env=replace_env)
            self.assertEqual(dict(os.environ), before)
        return result

    def observed(self):
        return json.loads((self.root / 'observed.json').read_text())

    def test_subscription_uses_only_lane_auth_and_captured_context(self):
        result = self.run_lane()
        self.assertTrue(result['completed'], result)
        observed = self.observed()
        env, argv = observed['env'], observed['argv']
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], 'lane-test-token')
        self.assertEqual(observed['settings'], '{"language":"English"}')
        self.assertEqual(observed['skill'], 'captured context')
        self.assertNotEqual(env['HOME'], str(self.home))
        self.assertFalse(Path(env['HOME']).exists())
        self.assertEqual((self.home / '.claude/skills/canary/SKILL.md').read_text(), 'captured context')
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.config))
        for name in ('AMBIENT_SENTINEL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
                     'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'AWS_ACCESS_KEY_ID',
                     'ZAI_API_KEY', 'OPENBENCH_PROXY', 'NODE_OPTIONS'):
            self.assertNotIn(name, env)
        self.assertNotIn('--bare', argv)
        self.assertIn('--verbose', argv)
        self.assertIn('stream-json', argv)

    def test_subscription_drops_conflicting_explicit_credentials_and_routes(self):
        explicit = {**self.ambient, **self.explicit}
        self.assertTrue(self.run_lane(explicit=explicit, replace_env=False)['completed'])
        env = self.observed()['env']
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                     'CLAUDE_CODE_USE_BEDROCK', 'AWS_ACCESS_KEY_ID', 'ZAI_API_KEY',
                     'OPENBENCH_PROXY', 'NODE_OPTIONS'):
            self.assertNotIn(name, env)
        self.assertEqual(env['CLAUDE_CODE_OAUTH_TOKEN'], 'lane-test-token')

    def test_subscription_never_falls_back_to_daily_token(self):
        explicit = dict(self.explicit)
        del explicit['CLAUDE_CODE_OAUTH_TOKEN']
        result = self.run_lane(explicit=explicit, replace_env=False)
        self.assertFalse(result['completed'])
        self.assertIn('CLAUDE_CODE_OAUTH_TOKEN', result['error'])
        self.assertFalse((self.root / 'observed.json').exists())

    def test_replace_env_requires_explicit_config(self):
        explicit = dict(self.explicit)
        del explicit['CLAUDE_CONFIG_DIR']
        result = self.run_lane(explicit=explicit)
        self.assertFalse(result['completed'])
        self.assertIn('CLAUDE_CONFIG_DIR', result['error'])
        self.assertFalse((self.root / 'observed.json').exists())

    def test_subscription_rejects_vendor_model(self):
        result = self.run_lane(model='deepseek-v4-flash')
        self.assertFalse(result['completed'])
        self.assertIn('subscription', result['error'])
        self.assertFalse((self.root / 'observed.json').exists())

    def test_api_route_retains_bare_vendor_routing(self):
        explicit = {**self.explicit, 'OPENBENCH_CLAUDE_AUTH_MODE': 'api', 'DEEPSEEK_API_KEY': 'lane-api-key'}
        result = self.run_lane(explicit=explicit, model='deepseek-v4-flash')
        self.assertTrue(result['completed'])
        observed = self.observed()
        self.assertIn('--bare', observed['argv'])
        self.assertEqual(observed['env']['ANTHROPIC_API_KEY'], 'lane-api-key')
        self.assertEqual(observed['env']['ANTHROPIC_BASE_URL'], 'https://api.deepseek.com/anthropic')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', observed['env'])
        self.assertEqual(result['final_message'], 'finished')
        self.assertIsNone(result['tool_events'])  # JSON does not observe tool traffic.

    def test_replace_env_api_cannot_fall_back_to_ambient_key(self):
        explicit = {**self.explicit, 'OPENBENCH_CLAUDE_AUTH_MODE': 'api'}
        result = self.run_lane(explicit=explicit)
        self.assertFalse(result['completed'])
        self.assertIn('ANTHROPIC_API_KEY', result['error'])
        self.assertFalse((self.root / 'observed.json').exists())

    def test_unknown_auth_mode_fails_before_launch(self):
        result = self.run_lane(explicit={**self.explicit, 'OPENBENCH_CLAUDE_AUTH_MODE': 'subscripton'})
        self.assertFalse(result['completed'])
        self.assertIn('OPENBENCH_CLAUDE_AUTH_MODE', result['error'])
        self.assertFalse((self.root / 'observed.json').exists())

    def test_subscription_rejects_configured_api_helper(self):
        (self.config / 'settings.json').write_text(json.dumps({'apiKeyHelper': 'false'}))
        result = self.run_lane()
        self.assertFalse(result['completed'])
        self.assertIn('subscription settings', result['error'])
        self.assertFalse((self.root / 'observed.json').exists())

    def test_subscription_rejects_routing_from_each_settings_surface(self):
        paths = [self.config / 'settings.json',
                 self.home / '.claude/settings.json',
                 self.root / '.claude/settings.json',
                 self.root / '.claude/settings.local.json']
        for path in paths:
            with self.subTest(surface=str(path.relative_to(self.root))):
                path.parent.mkdir(exist_ok=True)
                previous = path.read_text() if path.exists() else None
                path.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://invalid.example'}}))
                result = self.run_lane()
                self.assertFalse(result['completed'])
                self.assertIn('subscription settings', result['error'])
                self.assertFalse((self.root / 'observed.json').exists())
                if previous is None:
                    path.unlink()
                else:
                    path.write_text(previous)

    def test_subscription_rejects_malformed_settings(self):
        (self.config / 'settings.json').write_text('{broken')
        result = self.run_lane()
        self.assertFalse(result['completed'])
        self.assertIn('subscription settings', result['error'])
        self.assertFalse((self.root / 'observed.json').exists())

    def test_subscription_rejects_symlink_project_settings_root(self):
        external = self.root / 'outside-project'
        external.mkdir()
        (external / 'settings.json').write_text('{"language":"English"}')
        project = self.root / '.claude'
        project.symlink_to(external, target_is_directory=True)
        result = self.run_lane()
        self.assertFalse(result['completed'])
        self.assertIn('subscription settings', result['error'])
        self.assertFalse((self.root / 'observed.json').exists())
        self.assertEqual((external / 'settings.json').read_text(), '{"language":"English"}')
        project.unlink()
        project.mkdir()
        (project / 'settings.json').write_text('{"language":"English"}')
        self.assertTrue(self.run_lane()['completed'])

    def test_stream_retains_tools_final_message_and_terminal_usage(self):
        result = self.run_lane()
        self.assertEqual(result['final_message'], 'finished')
        self.assertEqual([event['type'] for event in result['tool_events']], ['assistant', 'user'])
        self.assertEqual(result['tool_events'][0]['message']['content'][0]['name'], 'Read')
        self.assertEqual(result['tokens'], 7)
        self.assertEqual(result['turns'], 2)
        self.assertIn('cleanup', result['full_output'])
        self.assertIsNone(result.get('evidence_error'))

    def test_missing_terminal_result_is_detectable(self):
        result = self.run_lane(explicit={**self.explicit, 'FIXTURE_MODE': 'missing_result'})
        self.assertFalse(result['completed'])
        self.assertIn('result', result['evidence_error'])
        self.assertEqual(result['final_message'], 'finished')
        self.assertEqual(len(result['tool_events']), 2)

    def test_malformed_event_is_detectable_without_losing_raw_evidence(self):
        result = self.run_lane(explicit={**self.explicit, 'FIXTURE_MODE': 'malformed'})
        self.assertFalse(result['completed'])
        self.assertIn('malformed', result['evidence_error'])
        self.assertIn('{broken event', result['full_output'])

    def test_stream_schema_errors_are_detectable_even_with_success_result(self):
        result = {'type': 'result', 'is_error': False, 'result': 'finished'}
        malformed = [
            {}, {'type': 7},
            {'type': 'assistant', 'message': {'content': {'type': 'tool_use'}}},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'r', 'name': 'Read'}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'r', 'name': 'Read', 'input': 'bad'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'text'}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 42}]}},
        ]
        for event in malformed:
            with self.subTest(event=event):
                _, evidence = claude._evidence(json.dumps(event) + '\n' + json.dumps(result), stream=True)
                self.assertIsNotNone(evidence['evidence_error'])

    def test_unknown_typed_stream_event_does_not_hide_complete_evidence(self):
        stream = '\n'.join(map(json.dumps, [
            {'type': 'future_event', 'payload': {'new': 'schema'}},
            {'type': 'result', 'is_error': False, 'result': 'finished'},
        ]))
        _, evidence = claude._evidence(stream, stream=True)
        self.assertIsNone(evidence['evidence_error'])
        self.assertEqual(evidence['final_message'], 'finished')

    def test_success_result_requires_string_final_message(self):
        for value in (None, 42, {'text': 'finished'}):
            with self.subTest(value=value):
                stream = json.dumps({'type': 'result', 'is_error': False, 'result': value})
                _, evidence = claude._evidence(stream, stream=True)
                self.assertIsNotNone(evidence['evidence_error'])

    def test_timeout_retains_partial_tool_evidence(self):
        # Allow cold interpreter startup before the fixture's five-second stall.
        # A 200 ms cap can kill it before it emits any partial evidence.
        result = self.run_lane(explicit={**self.explicit, 'FIXTURE_MODE': 'timeout'}, timeout=2)
        self.assertFalse(result['completed'])
        self.assertIn('timeout', result['error'])
        self.assertEqual(len(result['tool_events']), 2)
        self.assertIn('tool_result', result['full_output'])

    def test_captured_home_symlink_is_refused_without_launch(self):
        (self.home / 'escape').symlink_to(self.config, target_is_directory=True)
        result = self.run_lane()
        self.assertFalse(result['completed'])
        self.assertIn('captured', result['error'].lower())
        self.assertFalse((self.root / 'observed.json').exists())


if __name__ == '__main__':
    unittest.main()
