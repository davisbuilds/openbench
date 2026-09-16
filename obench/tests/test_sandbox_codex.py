"""Pinned Codex transport configuration; full runtime probes exercise the boundary."""
import unittest
from obench.harbor_agents.sandbox_codex import codex_config, _build_agent_class
from obench.harbor_results import _validate_openbench_task_content_digest
from obench.harbor_profiles import resolve_harbor_profile


class SandboxCodexTests(unittest.TestCase):
    def test_model_provider_has_only_loopback_transport(self):
        cfg=codex_config()
        provider=cfg['model_providers'][cfg['model_provider']]
        self.assertEqual(provider['base_url'], 'http://127.0.0.1:8765')
        self.assertFalse(provider['supports_websockets'])
        self.assertEqual(cfg['web_search'], 'disabled')
        self.assertFalse(cfg['features']['apps'])
        for port in (True,0,65536,'8765'):
            with self.assertRaises(ValueError): codex_config(port)

    def test_auth_can_never_fall_back_into_solver(self):
        class Base:
            def _get_env(self,key): return 'a-real-secret-if-this-escaped'
        cls=_build_agent_class(Base)
        obj=object.__new__(cls)
        self.assertIsNone(obj._resolve_auth_json_path())
        self.assertIsNone(obj._get_env('CODEX_AUTH_JSON_PATH'))
        self.assertIsNone(obj._get_env('CODEX_FORCE_AUTH_JSON'))
        self.assertEqual(obj._get_env('OPENAI_API_KEY'),'openbench-sandbox-placeholder')

    def test_existing_stock_treatments_are_unchanged(self):
        for base in ('gpt-5.6-terra','gpt-5.6-luna'):
            profile=resolve_harbor_profile('codex',base)
            self.assertEqual(profile.cli_version,'0.144.5')
            self.assertEqual(dict(profile.flags)['reasoning_effort'],'medium')

    def test_sandbox_digest_cannot_enter_ordinary_export_lane(self):
        value={'scheme':3,'sha256':'a'*64}
        with self.assertRaises(ValueError):
            _validate_openbench_task_content_digest(value,'test')
        self.assertEqual(_validate_openbench_task_content_digest(value,'test',allow_sandbox=True),value)
