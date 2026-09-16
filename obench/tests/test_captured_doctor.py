"""Captured preflight reports only the explicitly declared credential lane."""
import types
import unittest
from obench import doctor
from obench.tests.test_doctor import FakeProbes


class CapturedDoctorTests(unittest.TestCase):
    def candidate(self, base='codex', **kw):
        values=dict(name='captured', base_adapter=base, kind='config-variant',
                    captured_context=True, config_dir='/gone', config_files=['config.toml'],
                    config_contents={'config.toml': b''}, auth_files=[], pass_env=[],
                    env={'CODEX_HOME': '{config_dir}/codex'})
        values.update(kw)
        return types.SimpleNamespace(**values)

    def auth_row(self, candidate, p):
        rows,_=doctor.evaluate_candidate(candidate,'test-model',p,pins={})
        return next(r for r in rows if r['check']=='AUTH')

    def test_daily_codex_auth_does_not_satisfy_captured_lane(self):
        import os
        p=FakeProbes(exists_set=[os.path.expanduser('~/.codex/auth.json')])
        self.assertFalse(self.auth_row(self.candidate(),p)['ok'])
        c=self.candidate(auth_files=[{'source':'~/experiment/auth.json','destination':'codex/auth.json'}])
        p.exists_set.add(os.path.expanduser('~/experiment/auth.json'))
        self.assertTrue(self.auth_row(c,p)['ok'])
        c.auth_files[0]['destination']='wrong/auth.json'
        self.assertFalse(self.auth_row(c,p)['ok'])

    def test_subscription_token_must_be_explicitly_passed(self):
        c=self.candidate('claude', env={'CLAUDE_CONFIG_DIR':'{config_dir}',
                         'OPENBENCH_CLAUDE_AUTH_MODE':'subscription'})
        p=FakeProbes(env_map={'CLAUDE_CODE_OAUTH_TOKEN':'dummy','ANTHROPIC_API_KEY':'daily'})
        self.assertFalse(self.auth_row(c,p)['ok'])
        c.pass_env=['CLAUDE_CODE_OAUTH_TOKEN']
        self.assertTrue(self.auth_row(c,p)['ok'])
        p.env_map.pop('CLAUDE_CODE_OAUTH_TOKEN')
        self.assertFalse(self.auth_row(c,p)['ok'])

    def test_capture_preflight_rejects_bridge_models(self):
        for base in ('codex', 'claude'):
            with self.subTest(base=base):
                c=self.candidate(base, env={'OPENBENCH_CLAUDE_AUTH_MODE':'subscription'})
                p=FakeProbes(models_map={base: ({'native': 'native'}, {'bridge': {'model_id':'bridge'}})})
                rows,_=doctor.evaluate_candidate(c,'bridge',p,pins={})
                self.assertFalse(next(r for r in rows if r['check']=='MODEL')['ok'])
                rows,_=doctor.evaluate_candidate(c,'native',p,pins={})
                self.assertTrue(next(r for r in rows if r['check']=='MODEL')['ok'])

    def test_capture_config_preflight_uses_captured_bytes(self):
        c=self.candidate()
        rows,_=doctor.evaluate_candidate(c,'test-model',FakeProbes(),pins={})
        config=next(r for r in rows if r['check']=='CONFIG')
        self.assertTrue(config['ok'])
        self.assertIn('captured', config['detail'])


if __name__=='__main__':
    unittest.main()
