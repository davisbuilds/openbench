"""The opt-in repair sandbox cannot compile into the ordinary host-network route."""
import json
import shutil
from pathlib import Path
import tempfile
import unittest

from obench import init, suite_run
from obench.suite import SuiteError


class SandboxSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        init.init_scaffold(self.root)
        self.path = self.root / '.openbench/suites/default.toml'
        self.base = self.path.read_text().replace('gpt-5.6-sol', 'gpt-5.6-terra-xhigh')
        self.task_root = self.root / '.openbench/tasks'
        shutil.rmtree(self.task_root)
        source = Path(__file__).resolve().parents[2] / 'harbor-tasks-local/dojo-evidence-pr60-v3'
        shutil.copytree(source, self.task_root / source.name)

    def compile(self, extra=''):
        self.path.write_text(self.base + '\n[sandbox]\nkind = "repair-v1"\n'
                             'runtime_image = "sha256:' + 'a' * 64 + '"\n' + extra)
        return suite_run.compile_suite(self.path)

    def test_canonical_job_uses_confined_environment_and_trusted_grader(self):
        compiled = self.compile('max_requests = 37\n')
        job = suite_run.plan_jobs(compiled)[0]
        config = job.artifact.as_dict()
        self.assertEqual(config['environment']['import_path'], 'obench.harbor_sandbox:RepairSandbox')
        self.assertEqual(config['environment']['kwargs']['max_requests'], 37)
        self.assertEqual(config['environment']['kwargs']['request_timeout_seconds'], 1200)
        self.assertEqual(compiled.manifest['sandbox']['request_timeout_seconds'], 1200)
        self.assertEqual(config['verifier']['import_path'], 'obench.sandbox_grading:RepairVerifier')
        self.assertEqual(config['agents'][0]['import_path'], 'obench.harbor_agents.sandbox_codex:SandboxCodex')
        self.assertEqual(config['agents'][0]['kwargs']['version'], '0.154.0')
        self.assertEqual(config['agents'][0]['kwargs']['reasoning_effort'], 'xhigh')
        self.assertEqual(config['agents'][0]['model_name'], 'gpt-5.6-terra')
        self.assertEqual(compiled.manifest['sandbox']['kind'], 'repair-v1')
        self.assertNotIn('host.docker.internal', json.dumps(config))
        self.assertNotIn('extra_allowed_hosts', config['agents'][0])

    def test_policy_change_changes_suite_and_job_identity(self):
        a = self.compile('max_requests = 10\n')
        b = self.compile('max_requests = 11\n')
        self.assertNotEqual(a.manifest_sha256, b.manifest_sha256)
        self.assertNotEqual(suite_run.plan_jobs(a)[0].artifact.sha256,
                            suite_run.plan_jobs(b)[0].artifact.sha256)

    def test_rejects_mutable_image_and_policy_escape(self):
        for replacement in ('runtime_image = "python:latest"', 'kind = "public"'):
            self.compile()
            text = self.path.read_text()
            marker = 'runtime_image = ' if replacement.startswith('runtime') else 'kind = "repair-v1"'
            if marker == 'runtime_image = ':
                text = '\n'.join(replacement if line.startswith(marker) else line for line in text.splitlines())
            else:
                text = text.replace(marker, replacement)
            self.path.write_text(text)
            with self.assertRaises(SuiteError):
                suite_run.compile_suite(self.path)
        with self.assertRaises(SuiteError):
            self.compile('allowed_hosts = ["host.docker.internal"]\n')

    def test_rejects_non_codex_and_public_publication(self):
        self.base = self.base.replace('scope = "local_only"', 'scope = "public"')
        with self.assertRaisesRegex(suite_run.SuiteRunError, 'local_only'):
            self.compile()

    def test_rejects_unbound_or_unsupported_task(self):
        source = self.task_root / 'dojo-evidence-pr60-v3'
        (source / 'instruction.md').write_text('Changed task without resealing')
        with self.assertRaisesRegex(suite_run.SuiteRunError, 'binding mismatch'):
            self.compile()
        source.rename(self.task_root / 'another-task')
        with self.assertRaisesRegex(suite_run.SuiteRunError, 'only dojo'):
            self.compile()

    def test_v4_task_compiles_and_task_identity_cannot_select_legacy_oracle(self):
        from obench.sandbox_grading import task_digest
        shutil.rmtree(self.task_root / 'dojo-evidence-pr60-v3')
        source = Path(__file__).resolve().parents[2] / 'harbor-tasks-local/dojo-evidence-pr60-v4'
        target = self.task_root / source.name
        shutil.copytree(source, target)
        compiled = self.compile()
        self.assertEqual(compiled.task_sets[0].task_names, ('dojo-evidence-pr60-v4',))
        config = target / 'task.toml'
        config.write_text(config.read_text().replace(
            'openbench_task = "dojo-evidence-pr60-v4"', 'openbench_task = "dojo-evidence-pr60-v3"'))
        import re
        config.write_text(re.sub(r'(scheme = 3\nsha256 = ")[a-f0-9]{64}',
                                lambda m: m[1] + task_digest(target), config.read_text()))
        with self.assertRaisesRegex(suite_run.SuiteRunError, 'identity differs'):
            self.compile()

    def test_ordinary_suite_keeps_existing_route(self):
        self.path.write_text(self.base.replace('gpt-5.6-terra-xhigh', 'gpt-5.6-terra'))
        compiled = suite_run.compile_suite(self.path)
        config = suite_run.plan_jobs(compiled)[0].artifact.as_dict()
        self.assertNotIn('environment', config)
        self.assertNotIn('sandbox', compiled.manifest)
        self.assertEqual(config['agents'][0]['kwargs']['version'], '0.144.5')
