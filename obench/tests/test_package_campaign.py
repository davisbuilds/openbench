import json
from pathlib import Path
import tempfile
import unittest

from obench import evaluation_package, package_campaign, suite
from obench.frozen_context import _write
from obench.tests.test_evaluation_package import fixture


class CampaignPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        files = fixture()
        self.archive = self.root / 'evaluation.tar'
        self.sha = _write(files, {n: 0o644 for n in files}, self.archive, 'evaluation', {})
        self.auth = package_campaign.pin(self.archive, self.sha, True)
        self.task = self.root / 'synthetic-control-c1-o1'
        self.exported = package_campaign.export_task(self.auth.load(), self.task)

    def test_operator_suite_accepts_and_binds_explicit_pin(self):
        policy = suite._parse_sandbox({'kind': 'repair-v1', 'runtime_image': 'sha256:' + 'a'*64,
            'evaluation_archive': str(self.archive), 'evaluation_sha256': self.sha, 'trust_evaluator': True})
        self.assertEqual(policy.evaluation.sha256, self.sha)
        self.assertEqual(policy.evaluation.archive, self.archive)

    def test_metadata_never_authorizes_evaluation(self):
        with self.assertRaisesRegex(ValueError, 'operator evaluation pin'):
            package_campaign.validate_task_binding(self.task, self.exported['task_binding'], None)
        for args in ((self.archive, self.sha, False), (None, self.sha, True), (self.archive, None, True)):
            with self.assertRaises(ValueError):
                package_campaign.pin(*args)

    def test_projection_and_pin_are_independently_enforced(self):
        binding = self.exported['task_binding']
        package_campaign.validate_task_binding(self.task, binding, self.auth)
        (self.task / 'environment/app/web/index.html').write_text('substituted')
        with self.assertRaisesRegex(ValueError, 'projection'):
            package_campaign.validate_task_binding(self.task, binding, self.auth)

    def test_export_has_no_hidden_content_and_never_executes(self):
        files = fixture(); files['evaluator.py'] = b'raise RuntimeError("must not execute")\n'
        archive = self.root / 'nonexecuting.tar'
        sha = _write(files, {n: 0o644 for n in files}, archive, 'evaluation', {})
        task = self.root / 'other'
        package_campaign.export_task(evaluation_package.load(archive, sha), task)
        payload = b''.join(p.read_bytes() for p in task.rglob('*') if p.is_file())
        self.assertNotIn(b'synthetic-hidden-fixture', payload)
        self.assertNotIn(b'must not execute', payload)
        self.assertEqual((task/'environment/app/web/index.html').read_bytes(), files['solver/workspace/web/index.html'])

    def test_changed_archive_is_rejected(self):
        self.archive.write_bytes(self.archive.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            package_campaign.validate_task_binding(self.task, self.exported['task_binding'], self.auth)

    def compile(self, authorized=True):
        import shutil
        from obench import init, suite_run
        project = self.root/'project'
        project.mkdir()
        init.init_scaffold(project)
        tasks = project/'.openbench/tasks'
        shutil.rmtree(tasks)
        shutil.copytree(self.task, tasks/self.task.name)
        path = project/'.openbench/suites/default.toml'
        text = path.read_text().replace('gpt-5.6-sol','gpt-6.1-sol-high')
        text += '\n[sandbox]\nkind="repair-v1"\nruntime_image="sha256:' + 'a'*64 + '"\n'
        if authorized:
            text += 'evaluation_archive='+json.dumps(str(self.archive))+'\nevaluation_sha256="'+self.sha+'"\ntrust_evaluator=true\n'
        path.write_text(text)
        return suite_run.compile_suite(path)

    def test_v3_campaign_requires_its_observer_fingerprint(self):
        import copy
        from obench import stats
        files = fixture()
        descriptor = json.loads(files['evaluation.json'])
        descriptor.update(backend='activity-explorer-v3', oracle_revision=2)
        files['evaluation.json'] = json.dumps(descriptor).encode()
        self.archive = self.root/'v3.tar'
        self.sha = _write(files, {n: 0o644 for n in files}, self.archive, 'evaluation', {})
        self.auth = package_campaign.pin(self.archive, self.sha, True)
        self.task = self.root/'synthetic-control-c1-o2'
        package_campaign.export_task(self.auth.load(), self.task)
        compiled = self.compile()
        stats._validate_suite_manifest_shape(compiled.manifest, compiled.manifest_sha256)
        tampered = copy.deepcopy(compiled.manifest)
        for name in ('obench.browser_worker_v3', 'obench.repair_oracles.activity_explorer_v3'):
            tampered['sandbox']['implementation_sha256'].pop(name)
        with self.assertRaises(ValueError):
            stats._validate_suite_sandbox_policy(tampered)

    def test_browser_package_backends_select_browser_admission(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from obench import runtime_admission, suite_run
        base = self.root
        for backend, oracle_revision in (('activity-explorer-v2', 1), ('activity-explorer-v3', 2)):
            with self.subTest(backend=backend):
                self.root = base/backend
                self.root.mkdir()
                files = fixture()
                descriptor = json.loads(files['evaluation.json'])
                descriptor.update(backend=backend, oracle_revision=oracle_revision)
                files['evaluation.json'] = json.dumps(descriptor).encode()
                self.archive = self.root/'evaluation.tar'
                self.sha = _write(files, {n: 0o644 for n in files}, self.archive, 'evaluation', {})
                self.auth = package_campaign.pin(self.archive, self.sha, True)
                self.task = self.root/f'synthetic-control-c1-o{oracle_revision}'
                package_campaign.export_task(self.auth.load(), self.task)
                compiled = self.compile()
                with patch.object(runtime_admission, 'preflight_harbor_binary',
                                  return_value=SimpleNamespace(version='test', git_commit='test')), \
                     patch.object(suite_run, '_verify_sandbox_runtime'), \
                     patch.object(runtime_admission.subprocess, 'check_output', side_effect=[json.dumps([{
                         'Id': compiled.suite.sandbox.runtime_image, 'Os': 'linux', 'Architecture': 'arm64'}]), '{}']):
                    fingerprint = runtime_admission.fingerprint(compiled, '/test/harbor')
                self.assertEqual(fingerprint.get('execution_profile'), 'browser-v1')
                _, task = runtime_admission.prepare_control(compiled, self.root/'control')
                self.assertEqual(task.name, 'activity-explorer-c2-o2')

    def test_canonical_job_binds_pin_and_manifest_is_readable(self):
        from obench import suite_run, stats
        compiled = self.compile()
        job = suite_run.plan_jobs(compiled)[0].artifact.as_dict()
        self.assertEqual(job['verifier']['import_path'], 'obench.package_campaign:PackageVerifier')
        for section in ('environment','verifier'):
            kw=job[section]['kwargs']
            self.assertEqual(kw['evaluation_sha256'], self.sha)
            self.assertEqual(kw['evaluation_archive'], str(self.archive))
            self.assertIs(kw['trust_evaluator'], True)
        self.assertEqual(compiled.manifest['sandbox']['evaluation_package']['sha256'], self.sha)
        self.assertNotIn(str(self.archive), json.dumps(compiled.manifest))
        stats._validate_suite_manifest_shape(compiled.manifest, compiled.manifest_sha256)

    def test_canonical_compile_refuses_metadata_without_operator_pin(self):
        from obench.suite_run import SuiteRunError
        with self.assertRaisesRegex(SuiteRunError, 'operator evaluation pin'):
            self.compile(False)

    def test_quality_inspection_requires_the_same_authority(self):
        from obench.repair_validation import inspect_task
        with self.assertRaisesRegex(ValueError, 'operator evaluation pin'):
            inspect_task(self.task)
        info=inspect_task(self.task,evaluation=self.auth)
        self.assertEqual(info['evaluation_package']['sha256'],self.sha)
        self.assertEqual(info['checks'],[{'id':'list','bucket':'content'}])
        self.assertTrue(info['browser'])

    def test_resealing_cannot_change_approved_solver_permissions(self):
        for relative in ('instruction.md', 'environment/app/web/index.html'):
            with self.subTest(relative=relative):
                path=self.task/relative
                previous=path.stat().st_mode & 0o777
                path.chmod(previous ^ 0o111)
                try:
                    # Recomputing a task seal is not authority to change its
                    # package-approved execution environment.
                    with self.assertRaisesRegex(ValueError, 'projection'):
                        package_campaign.task_manifest(self.task,self.auth)
                finally:
                    path.chmod(previous)
