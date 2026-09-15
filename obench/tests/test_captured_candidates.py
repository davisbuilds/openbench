"""Captured candidates use frozen bytes and an explicit per-dispatch environment."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from obench.candidates import load_candidate


class CapturedCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/settings').write_text('captured sentinel')
        (self.root / 'adapters').mkdir()
        self.adapter('''def run(instruction, workdir, model, timeout_s, env_override=None, replace_env=False):
    import json, os
    from pathlib import Path
    return {"completed": True, "env": env_override, "replaced": replace_env,
            "global_sentinel": os.environ.get("CAPTURE_SENTINEL"),
            "settings": Path(env_override["CODEX_HOME"], "settings").read_text(),
            "home_exists": Path(env_override["OPENBENCH_CAPTURED_HOME"]).is_dir()}
''')

    def adapter(self, text):
        (self.root / 'adapters/codex.py').write_text(text)

    def candidate(self, extra='', env_extra=''):
        p = self.root / 'candidate.toml'
        p.write_text('kind="config-variant"\nname="captured"\nbase_adapter="codex"\n'
                     'config_dir="config"\nconfig_files=["settings"]\n'
                     'captured_context=true\n' + extra + '\n[env]\n'
                     'CODEX_HOME="{config_dir}"\nCAPTURE_SENTINEL="child"\n' + env_extra)
        return load_candidate(p, self.root / 'adapters')

    def test_capture_replaces_environment_without_mutating_parent(self):
        with patch.dict(os.environ, {'CAPTURE_SENTINEL': 'parent', 'DAILY_SECRET': 'dummy',
                                    'EXPERIMENT_TOKEN': 'lane'}, clear=True):
            candidate = self.candidate('pass_env=["EXPERIMENT_TOKEN"]')
            (self.root / 'config/settings').write_text('changed live source')
            result = candidate.run('task', str(self.root), 'model', 5)
            self.assertEqual(result['settings'], 'captured sentinel')
            self.assertEqual(result['global_sentinel'], 'parent')
            self.assertEqual(os.environ['CAPTURE_SENTINEL'], 'parent')
            self.assertNotIn('DAILY_SECRET', result['env'])
            self.assertEqual(result['env']['EXPERIMENT_TOKEN'], 'lane')
            self.assertEqual(result['env']['CAPTURE_SENTINEL'], 'child')
            self.assertTrue(result['replaced'])
            self.assertTrue(result['home_exists'])
            self.assertTrue(candidate.REQUIRE_EVIDENCE)

    def test_capture_requires_direct_environment_support_before_dispatch(self):
        self.adapter('def run(instruction, workdir, model, timeout_s):\n    raise AssertionError("dispatched")\n')
        with self.assertRaisesRegex(ValueError, 'env_override.*replace_env'):
            self.candidate()

    def test_capture_rejects_inherited_environment(self):
        with self.assertRaisesRegex(ValueError, 'inherit_env'):
            self.candidate('inherit_env=true')

    def test_capture_rejects_live_configuration_root(self):
        with self.assertRaisesRegex(ValueError, 'CODEX_HOME.*config_dir'):
            self.candidate()  # valid control
            self.candidate_with_live_root()

    def candidate_with_live_root(self):
        self.candidate()
        p=self.root/'candidate.toml'
        p.write_text(p.read_text().replace('CODEX_HOME="{config_dir}"', 'CODEX_HOME="/daily/config"'))
        return load_candidate(p, self.root/'adapters')

    def test_home_must_be_staged_as_captured_assets(self):
        with self.assertRaisesRegex(ValueError, 'HOME'):
            self.candidate(env_extra='HOME="/daily/home"')

    def test_capture_disallows_secret_literals(self):
        with self.assertRaisesRegex(ValueError, 'pass_env'):
            self.candidate(env_extra='CLAUDE_CODE_OAUTH_TOKEN="dummy-secret"')

    def test_pass_env_is_typed_and_cannot_inject_home(self):
        for extra in ('pass_env="TOKEN"', 'pass_env=["HOME"]', 'captured_context="true"'):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                if extra.startswith('captured_context'):
                    self.candidate()
                    p=self.root/'candidate.toml'
                    p.write_text(p.read_text().replace('captured_context=true', extra))
                    load_candidate(p,self.root/'adapters')
                else:
                    self.candidate(extra)

    def test_capture_rejects_duplicate_auth_destinations(self):
        with self.assertRaisesRegex(ValueError, 'duplicate.*auth'):
            self.candidate('auth_files=[{source="~/lane/a",destination="auth.json"},'
                           '{source="~/lane/b",destination="auth.json"}]')

    def test_legacy_env_controls_are_not_silently_ignored(self):
        self.candidate('inherit_env=false')
        p=self.root/'candidate.toml'
        p.write_text(p.read_text().replace('captured_context=true', 'captured_context=false'))
        with self.assertRaisesRegex(ValueError, 'require captured_context'):
            load_candidate(p,self.root/'adapters')

    def test_captured_claude_requires_skill_enabled_subscription_route(self):
        self.candidate()
        (self.root/'adapters/claude.py').write_text((self.root/'adapters/codex.py').read_text())
        p=self.root/'candidate.toml'
        p.write_text(p.read_text().replace('base_adapter="codex"','base_adapter="claude"')
                     .replace('CODEX_HOME=', 'CLAUDE_CONFIG_DIR='))
        with self.assertRaisesRegex(ValueError, 'subscription'):
            load_candidate(p,self.root/'adapters')
        p.write_text(p.read_text()+'\nOPENBENCH_CLAUDE_AUTH_MODE="subscription"\n')
        self.assertTrue(load_candidate(p,self.root/'adapters').captured_context)

    def test_environment_isolated_on_adapter_exception(self):
        self.adapter('''def run(instruction, workdir, model, timeout_s, env_override=None, replace_env=False):
    raise RuntimeError("dispatch failed")
''')
        candidate=self.candidate()
        before=dict(os.environ)
        with self.assertRaisesRegex(RuntimeError, 'dispatch failed'):
            candidate.run('task',str(self.root),'model',5)
        self.assertEqual(dict(os.environ),before)


if __name__ == '__main__':
    unittest.main()
