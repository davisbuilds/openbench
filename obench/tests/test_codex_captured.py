"""Captured Codex configuration and local evidence contracts (offline)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from obench.adapters import codex


class CapturedCodexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.home = self.root / 'home'
        skill = self.home / '.agents/skills/captured/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('captured fixture')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        exe = self.bin / 'codex'
        exe.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
home = Path.home()
report = {'env': dict(os.environ), 'argv': sys.argv,
          'skills': [str(p.relative_to(home)) for p in home.glob('.agents/skills/*/SKILL.md')]}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(report)}}))
print(json.dumps({'type':'turn.completed','usage':{}}))
''')
        exe.chmod(0o755)
        self.env = {'PATH': str(self.bin), 'CODEX_HOME': str(self.config),
                    'OPENBENCH_CAPTURED_HOME': str(self.home)}

    def test_replaces_environment_and_stages_only_requested_home(self):
        with mock.patch.dict(os.environ, {'AMBIENT_CANARY':'secret',
                'BENCH_IN_CONTAINER':'1', 'OPENBENCH_PROXY':'1',
                'OPENBENCH_PROXY_BASE_URL':'http://ambient',
                'OPENBENCH_PROXY_CELL_TOKEN':'secret'}):
            before = dict(os.environ)
            result = codex.run('fixture', str(self.root), 'gpt-6-astra-max', 5,
                               env_override=self.env, replace_env=True)
            self.assertEqual(dict(os.environ), before)
        self.assertTrue(result['completed'])
        self.assertIsNone(result['evidence_error'])
        report = json.loads(result['final_message'])
        self.assertNotIn('AMBIENT_CANARY', report['env'])
        self.assertNotIn('OPENBENCH_CAPTURED_HOME', report['env'])
        self.assertEqual(report['env']['CODEX_HOME'], str(self.config))
        self.assertEqual(report['skills'], ['.agents/skills/captured/SKILL.md'])
        self.assertNotEqual(report['env']['HOME'], str(self.home))
        self.assertFalse(Path(report['env']['HOME']).exists())
        self.assertIn('workspace-write', report['argv'])
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', report['argv'])
        self.assertFalse(any('http://ambient' in a for a in report['argv']))
        self.assertIn('model_reasoning_effort="max"', report['argv'])
        self.assertIn('service_tier="default"', report['argv'])
        self.assertEqual(list(self.config.iterdir()), [])

    def test_missing_explicit_config_rejected_without_daily_auth_access(self):
        with mock.patch.object(codex, 'auth_file_lease', side_effect=AssertionError('ambient auth read')):
            result = codex.run('fixture', str(self.root), 'gpt-5.5-medium', 5,
                               env_override={'PATH':str(self.bin)}, replace_env=True)
        self.assertFalse(result['completed'])
        self.assertIn('CODEX_HOME', result['error'])

    def test_captured_lane_rejects_relative_config_and_bridge_models(self):
        for env, model in [(dict(self.env, CODEX_HOME='~/.codex'), 'gpt-5.5-medium'),
                           (self.env, 'deepseek-v4-flash')]:
            with self.subTest(model=model):
                result = codex.run('fixture', str(self.root), model, 5,
                                   env_override=env, replace_env=True)
                self.assertFalse(result['completed'])
                self.assertIn('SETUP-NEEDED', result['error'])

    def test_captured_home_rejects_symlinks(self):
        (self.home / 'ambient').symlink_to(self.config, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            codex.run('fixture', str(self.root), 'gpt-5.5-medium', 5,
                      env_override=self.env, replace_env=True)

    def test_astra_alias_identity_and_efforts(self):
        for effort in ('low', 'medium', 'high', 'xhigh', 'max'):
            name = 'gpt-6-astra-' + effort
            self.assertEqual(codex.model_identity(name),
                {'canonical_model':'gpt-6-astra', 'reasoning_effort':effort, 'is_open':False})
        self.assertEqual(codex.model_identity('gpt-6-astra')['reasoning_effort'], 'medium')

    def test_evidence_preserves_tool_lifecycle_and_final_message(self):
        events = [
            {'type':'item.completed','item':{'type':'agent_message','text':'earlier'}},
            {'type':'item.started','item':{'type':'command_execution','id':'tool1','command':'cat SKILL.md'}},
            {'type':'item.completed','item':{'type':'command_execution','id':'tool1','aggregated_output':'fixture skill','exit_code':0}},
            {'type':'item.completed','item':{'type':'agent_message','text':'final ' + 'x'*3000}},
        ]
        stream = 'null\n[]\nmalformed\n' + '\n'.join(map(json.dumps, events))
        parsed = codex._parse_evidence(stream)
        self.assertEqual(parsed['final_message'], events[-1]['item']['text'])
        self.assertEqual(parsed['tool_events'], events[1:3])

    def test_timeout_retains_structured_partial_evidence(self):
        import subprocess
        stream = json.dumps({'type':'item.started','item':{'type':'command_execution','id':'unfinished'}})
        with mock.patch.object(codex.subprocess, 'run', side_effect=subprocess.TimeoutExpired('codex', 5, output=stream.encode())):
            result = codex.run('fixture', str(self.root), 'gpt-5.5-medium', 5,
                               env_override=self.env, replace_env=True)
        self.assertFalse(result['completed'])
        self.assertIsNone(result['final_message'])
        self.assertEqual(result['tool_events'][0]['item']['id'], 'unfinished')
        self.assertEqual(result['full_output'], stream)

    def test_explicit_commentary_is_not_final_evidence(self):
        event = {'type':'item.completed','item':{'type':'agent_message',
                 'phase':'commentary', 'text':'Working on it'}}
        self.assertIsNone(codex._parse_evidence(json.dumps(event))['final_message'])

    def test_astra_missing_cache_writes_are_unknown(self):
        import subprocess
        event = {'type':'turn.completed', 'usage':{'input_tokens':20,
                 'cached_input_tokens':5, 'output_tokens':3, 'reasoning_output_tokens':1}}
        proc = subprocess.CompletedProcess(['codex'], 0, json.dumps(event), '')
        with mock.patch.object(codex.subprocess, 'run', return_value=proc):
            result = codex.run('fixture', str(self.root), 'gpt-6-astra', 5,
                               env_override=self.env, replace_env=True)
        self.assertEqual(result['token_basis'], 'estimated')
        self.assertIsNone(result['tokens_cache_write'])

    def test_evidence_rejects_truncated_and_unfinished_streams(self):
        message = json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}})
        terminal = json.dumps({'type':'turn.completed','usage':{}})
        for suffix, expected in [('', 'missing_turn_completed'),
                                 ('\n{"type":"item.', 'malformed_event_stream'),
                                 ('\n[]\n' + terminal, 'invalid_event_schema'),
                                 ('\n{"type":"item.completed","item":null}\n' + terminal, 'invalid_event_schema'),
                                 ('\n{"type":"turn.failed"}', 'failed_turn')]:
            with self.subTest(suffix=suffix):
                parsed = codex._parse_evidence(message + suffix)
                self.assertEqual(parsed['evidence_error'], expected)
                self.assertEqual(parsed['final_message'], 'done')
        self.assertIsNone(codex._parse_evidence(message + '\n' + terminal)['evidence_error'])
