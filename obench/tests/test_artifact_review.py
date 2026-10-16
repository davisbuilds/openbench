"""Fixed data observer controls; no inference or submitted source execution."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from obench.harbor_sandbox import pack_files
from obench.repair_oracles import artifact_review_v1 as review
from obench.tests.test_evaluation_package import artifact_fixture
from obench import evaluation_package, package_campaign
from obench.frozen_context import _write


FINDING = {'title': '[P1] Handle absent input', 'body': 'None raises before a result can be returned.',
           'file': 'src/example.py', 'line_start': 2, 'line_end': 2, 'priority': 1}


class ArtifactObserverTests(unittest.TestCase):
    def setUp(self):
        self.files = {'src/example.py': b'def normalize(value):\n    return value.strip()\n',
                      review.ARTIFACT: json.dumps({'findings': [FINDING]}).encode()}
        self.protected = {'src/example.py': hashlib.sha256(self.files['src/example.py']).hexdigest()}

    def observe(self, raw=None):
        files = dict(self.files)
        if raw is not None:
            files[review.ARTIFACT] = raw
        return review.observe(files, self.protected)

    def test_finding_and_clean_report_are_valid_without_invented_findings(self):
        self.assertEqual(self.observe(), {'ok': True, 'value': {'findings': [FINDING], 'scope_drift': []}})
        self.assertEqual(self.observe(b'{"findings":[]}')['value']['findings'], [])

    def test_invalid_report_is_candidate_miss_not_infrastructure(self):
        for raw in (b'{}', b'', b'{"findings":[],"findings":[]}', b'{"findings":[NaN]}',
                    b'{"findings":[],"reward":1}', b'x' * (review.MAX_ARTIFACT_BYTES + 1)):
            with self.subTest(raw=raw[:80]):
                result = self.observe(raw)
                self.assertIs(result['ok'], False)
                self.assertEqual(result['error']['operation'], 'review')
        files = dict(self.files); files.pop(review.ARTIFACT)
        self.assertFalse(review.observe(files, self.protected)['ok'])

    def test_locations_priorities_unknown_fields_and_size_are_checked(self):
        for changes in ({'file': '/etc/passwd'}, {'file': '../src/example.py'}, {'file': 'src//example.py'},
                        {'priority': True}, {'priority': 4}, {'line_start': 0}, {'line_end': 1},
                        {'line_start': False}, {'body': ''}, {'title': 'x' * 513}, {'reward': 1}):
            with self.subTest(changes=changes):
                raw = json.dumps({'findings': [{**FINDING, **changes}]}).encode()
                self.assertFalse(self.observe(raw)['ok'])
        self.assertFalse(self.observe(json.dumps({'findings': [FINDING] * 65}).encode())['ok'])

    def test_changed_added_removed_source_and_git_exclusions(self):
        files = dict(self.files)
        files['src/example.py'] = b'fixed'
        files['extra.py'] = b'unrequested'
        files['.git/config'] = b'generated git state'
        files['submission/note.txt'] = b'output scratch'
        result = review.observe(files, {**self.protected, 'removed.py': '0' * 64})
        self.assertEqual(result['value']['scope_drift'],
                         ['removed:removed.py', 'added:extra.py', 'changed:src/example.py'])

    def test_full_review_allows_build_outputs_but_protects_supplied_source(self):
        descriptor = b'{"schema":1,"patch":"review.patch"}'
        protected = {**self.protected, '.openbench-review.json': hashlib.sha256(descriptor).hexdigest()}
        files = {**self.files, '.openbench-review.json': descriptor, 'dist/example.js': b'compiled'}
        self.assertEqual(review.observe(files, protected)['value']['scope_drift'], [])
        files['src/example.py'] = b'changed'
        self.assertEqual(review.observe(files, protected)['value']['scope_drift'], ['changed:src/example.py'])
        del files['src/example.py']
        self.assertEqual(review.observe(files, protected)['value']['scope_drift'], ['removed:src/example.py'])
        # A solver cannot opt an older package into the new behavior.
        self.assertIn('added:.openbench-review.json', review.observe(files, self.protected)['value']['scope_drift'])

    def test_only_the_exact_runtime_git_pointer_is_exempt(self):
        files = {**self.files, '.git': b'gitdir: /tmp/openbench-workspace.git\n'}
        self.assertEqual(review.observe(files, self.protected)['value']['scope_drift'], [])
        for pointer in (b'gitdir: /other/history\n', b'gitdir: /tmp/openbench-workspace.git', b'changed'):
            with self.subTest(pointer=pointer):
                files['.git'] = pointer
                self.assertEqual(review.observe(files, self.protected)['value']['scope_drift'], ['added:.git'])

    def test_actual_fixed_worker_treats_source_as_data_and_returns_scope(self):
        import base64
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / 'executed'
            files = dict(self.files)
            files['src/example.py'] = ('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n').encode()
            request = {'source': base64.b64encode(pack_files(files)).decode(),
                       'cases': [{'protected_sha256': self.protected}]}
            result = subprocess.run([sys.executable, '-I', '-c', review.worker_program()],
                                    input=json.dumps(request), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)['results'][0]
            self.assertEqual(observed['value']['findings'], [FINDING])
            self.assertEqual(observed['value']['scope_drift'], ['changed:src/example.py'])
            self.assertFalse(sentinel.exists())


class ArtifactPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        files = artifact_fixture()
        self.archive = self.root / 'package.tar'
        self.sha = _write(files, {n: 0o644 for n in files}, self.archive, 'evaluation', {})
        self.authorization = package_campaign.pin(self.archive, self.sha, True)
        self.task = self.root / 'synthetic-control-c1-o1'
        package_campaign.export_task(self.authorization.load(), self.task)

    def test_inspection_and_source_policy_are_artifact_only(self):
        from obench.repair_validation import inspect_task, source_files
        info = inspect_task(self.task, evaluation=self.authorization)
        self.assertFalse(info['browser'])
        self.assertEqual(info['source_prefix'], '')
        files = source_files(self.task / 'environment/app', info)
        self.assertEqual(set(files), {'src/example.py', 'submission/review.json'})

    def test_trusted_evaluator_cannot_override_source_mutations(self):
        approved = self.authorization.load()
        checks = evaluation_package._checks(approved, [{'ok': True, 'value': {
            'findings': [], 'scope_drift': ['changed:src/example.py']}}], trust_evaluator=True)
        self.assertEqual(checks[0]['failure_reasons'], ['review-scope-drift'])
        checks = evaluation_package._checks(approved, [{'ok': False}], trust_evaluator=True)
        self.assertEqual(checks[0]['failure_reasons'], ['worker-operation-failed'])

    def test_canonical_compilation_binds_artifact_backend_and_manifest(self):
        import shutil
        from obench import init, suite_run, stats
        project = self.root / 'project'; project.mkdir(); init.init_scaffold(project)
        tasks = project / '.openbench/tasks'; shutil.rmtree(tasks)
        shutil.copytree(self.task, tasks / self.task.name)
        path = project / '.openbench/suites/default.toml'
        path.write_text(path.read_text().replace('gpt-5.6-sol', 'gpt-6.1-sol-high') +
            '\n[sandbox]\nkind="repair-v1"\nruntime_image="sha256:' + 'a' * 64 + '"\n' +
            'evaluation_archive=' + json.dumps(str(self.archive)) + '\nevaluation_sha256="' + self.sha + '"\ntrust_evaluator=true\n')
        compiled = suite_run.compile_suite(path)
        stats._validate_suite_manifest_shape(compiled.manifest, compiled.manifest_sha256)
        job = suite_run.plan_jobs(compiled)[0].artifact.as_dict()
        self.assertEqual(job['verifier']['import_path'], 'obench.package_campaign:PackageVerifier')
        self.assertEqual(compiled.manifest['sandbox']['evaluation_package']['evaluation']['backend'], 'artifact-review-v1')
        self.assertIn('obench.repair_oracles.artifact_review_v1', compiled.manifest['sandbox']['implementation_sha256'])
        from types import SimpleNamespace
        from unittest.mock import patch
        from obench import runtime_admission
        with patch.object(runtime_admission, 'preflight_harbor_binary', return_value=SimpleNamespace(version='test', git_commit='test')), \
             patch.object(suite_run, '_verify_sandbox_runtime'), \
             patch.object(runtime_admission.subprocess, 'check_output', side_effect=[json.dumps([{
                 'Id': compiled.suite.sandbox.runtime_image, 'Os': 'linux', 'Architecture': 'arm64'}]), '{}']):
            fingerprint = runtime_admission.fingerprint(compiled, '/test/harbor')
        self.assertNotIn('execution_profile', fingerprint)
        control, task = runtime_admission.prepare_control(compiled, self.root / 'control')
        self.assertIsNone(control.suite.sandbox.evaluation)
        self.assertEqual(task.name, 'dojo-evidence-pr60-v5')
        self.assertEqual(control.suite.run.timeout_seconds, 180)
