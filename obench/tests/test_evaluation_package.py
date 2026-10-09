"""Synthetic packages only: private evaluation material never belongs in tests."""
import json
import io
import os
from pathlib import Path
from contextlib import redirect_stdout
import tempfile
import unittest

from obench import evaluation_package as package
from obench.frozen_context import _write


def fixture():
    return {
        'evaluation.json': json.dumps({'schema': 1, 'id': 'synthetic-control',
            'case_revision': 1, 'oracle_revision': 1, 'split': 'development',
            'backend': 'activity-explorer-v2'}).encode(),
        'cases.json': json.dumps([{'id': 'list', 'bucket': 'content',
                                  'request': {'mode': 'list', 'data': []}}]).encode(),
        'evaluator.py': b'def grade(cases, observations, fixtures):\n    return [{"pass": True, "reasons": []} for _ in cases]\n',
        'solver/instruction.md': b'Implement an activity viewer.\n',
        'solver/workspace/web/index.html': b'<p>Baseline</p>',
        'fixtures/canary.txt': b'synthetic-hidden-fixture',
        'controls/reference/web/index.html': b'<p>Reference</p>',
    }


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def archive(self, files=None):
        files = fixture() if files is None else files
        path = self.root / 'package.tar'
        sha = _write(files, {n: 0o644 for n in files}, path, 'evaluation', {})
        return path, sha

    def test_only_closed_backend_not_module_from_package(self):
        files = fixture()
        descriptor = json.loads(files['evaluation.json'])
        descriptor['backend'] = 'candidate.supplied.module'
        files['evaluation.json'] = json.dumps(descriptor).encode()
        path, sha = self.archive(files)
        with self.assertRaisesRegex(ValueError, 'backend'):
            package.load(path, sha)

    def test_pin_is_required_and_verified(self):
        path, sha = self.archive()
        package.load(path, sha)
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            package.load(path, '0' * 64)

    def test_undeclared_roots_fail_closed(self):
        files = fixture(); files['auth.json'] = b'{}'
        path, sha = self.archive(files)
        with self.assertRaisesRegex(ValueError, 'package path'):
            package.load(path, sha)

    def test_inspect_and_export_never_execute_and_only_copy_solver_projection(self):
        files = fixture()
        files['evaluator.py'] = b'raise RuntimeError("must never execute during inspection")\n'
        path, sha = self.archive(files)
        approved = package.load(path, sha)
        self.assertFalse(approved.inspect()['evaluator_executed'])
        dest = self.root / 'solver'
        package.export(approved, dest)
        actual = {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob('*') if p.is_file()}
        self.assertEqual(actual, {n.removeprefix('solver/'): b for n, b in files.items() if n.startswith('solver/')})
        with self.assertRaises(ValueError):
            package.export(approved, dest)

    def test_freeze_is_deterministic_and_refuses_overwrite_and_links(self):
        root = self.root / 'source'; root.mkdir()
        for name, data in fixture().items():
            p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
        one, two = self.root / 'one.tar', self.root / 'two.tar'
        self.assertEqual(package.freeze(root, one), package.freeze(root, two))
        self.assertEqual(one.read_bytes(), two.read_bytes())
        self.assertEqual(one.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            package.freeze(root, one)
        (root / 'fixtures/link').symlink_to(root / 'evaluator.py')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            package.freeze(root, self.root / 'linked.tar')

    def test_archive_mutation_is_rejected(self):
        path, sha = self.archive()
        path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            package.load(path, sha)

    def test_no_trust_means_no_code_execution_or_runtime_access(self):
        files = fixture(); marker = self.root / 'executed'
        files['evaluator.py'] = f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'.encode()
        path, sha = self.archive(files)
        with self.assertRaisesRegex(ValueError, 'trust-evaluator'):
            package.replay(path, sha, self.root / 'absent-source', 'not-an-image')
        with self.assertRaisesRegex(ValueError, 'trust-evaluator'):
            package._checks(package.load(path, sha), [], trust_evaluator=False)
        self.assertFalse(marker.exists())

    def test_verdicts_cannot_rename_cases_or_override_worker_failure(self):
        files = fixture()
        files['evaluator.py'] = b'''def grade(cases, observations, fixtures):
    assert fixtures['canary.txt'] == b'synthetic-hidden-fixture'
    cases[0]['id'] = 'rewritten'
    observations[0]['ok'] = True
    print('private output')
    return [{'pass': True, 'reasons': []}]
'''
        path, sha = self.archive(files)
        output = io.StringIO()
        with redirect_stdout(output):
            checks = package._checks(package.load(path, sha), [{'ok': False}], trust_evaluator=True)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(checks, [{'id': 'list', 'bucket': 'content', 'pass': False,
                                   'failure_reasons': ['worker-operation-failed']}])

    def test_malformed_verdict_is_infrastructure_failure_not_zero_or_success(self):
        path, sha = self.archive()
        approved = package.load(path, sha)
        for value in ([], [True], [{'pass': 1, 'reasons': []}], [{'pass': False, 'reasons': []}],
                      [{'pass': True, 'reasons': ['failure']}], [{'pass': False, 'reasons': ['x' * 201]}]):
            with self.subTest(value=value):
                approved.archive.files['evaluator.py'] = ('def grade(*args):\n    return ' + repr(value)).encode()
                with self.assertRaises(ValueError):
                    package._checks(approved, [{'ok': True}], trust_evaluator=True)

    def test_duplicate_case_id_and_non_finite_json_rejected(self):
        for cases in (b'[{"id":"x","bucket":"a","request":{}},{"id":"x","bucket":"a","request":{}}]',
                      b'[{"id":"x","bucket":"a","request":{"number":NaN}}]'):
            with self.subTest(cases=cases):
                files = fixture(); files['cases.json'] = cases
                with self.assertRaises(ValueError):
                    package._validate(files)

    def test_evaluator_exit_zero_is_incomplete_not_success(self):
        files = fixture(); files['evaluator.py'] = b'raise SystemExit(0)\n'
        path, sha = self.archive(files)
        with self.assertRaisesRegex(ValueError, 'exited without completing'):
            package._checks(package.load(path, sha), [{'ok': True}], trust_evaluator=True)

    def test_cli_reports_failure_without_false_completion_and_help_is_discoverable(self):
        from obench.cli import main
        path, sha = self.archive()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['repair', 'package', 'inspect', str(path), '--sha256', sha, '--json'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out.getvalue())['campaign_eligible'])
        out = io.StringIO(); report = self.root / 'replay.json'
        with redirect_stdout(out):
            code = main(['repair', 'package', 'replay', str(path), '--sha256', sha,
                         '--source', str(self.root), '--image', 'sha256:' + 'a' * 64,
                         '--output', str(report), '--json'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'incomplete')
        self.assertFalse(report.exists())
        operation = json.loads(Path(str(report) + '.evidence/operation.json').read_text())
        self.assertEqual(operation['status'], 'incomplete')


@unittest.skipUnless(os.environ.get('OBENCH_BROWSER_IMAGE'), 'requires pinned browser Docker image')
class DockerPackageTests(unittest.TestCase):
    def test_exported_checkout_through_actual_sandbox_denies_host_package_and_replays(self):
        import asyncio
        from scripts.local.verify_evaluation_package import verify
        from scripts.ci.browser_quality_fixtures import HTML, JS, CSS
        from obench.repair_oracles.activity_explorer import cases
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            files = fixture()
            # Full ordinary dev checkout stays in the solver projection; only
            # the web source crosses the stopped-solver grading boundary.
            task = Path(__file__).resolve().parents[2] / 'benchmarks/harbor/local/activity-explorer-c2-o2'
            files = {n: b for n, b in files.items() if not n.startswith(('solver/workspace/', 'controls/'))}
            for p in (task / 'environment/app').rglob('*'):
                if p.is_file():
                    files['solver/workspace/' + p.relative_to(task / 'environment/app').as_posix()] = p.read_bytes()
            for name, body in [('index.html', HTML), ('app.js', JS), ('style.css', CSS)]:
                files['controls/reference/web/' + name] = body.encode()
            files['cases.json'] = json.dumps([{'id': n, 'bucket': 'content', 'request': r}
                                             for n, _, r in cases() if n in ('list', 'detail')]).encode()
            files['evaluator.py'] = b'''from obench.repair_oracles.activity_explorer_v2 import reasons
def grade(cases, observations, fixtures):
    verdicts = []
    for case, observed in zip(cases, observations, strict=True):
        failures = reasons(case['id'], observed.get('value', {}), case['request'])
        verdicts.append({'pass': not failures, 'reasons': failures})
    return verdicts
'''
            archive = root / 'evaluation.tar'
            sha = _write(files, {n: 0o644 for n in files}, archive, 'evaluation', {})
            result = asyncio.run(verify(archive, sha, os.environ['OBENCH_BROWSER_IMAGE'], root / 'evidence',
                                        trust_evaluator=True))
            self.assertEqual(result['status'], 'passed')
            self.assertEqual([r['score'] for r in result['controls']], [0, 1])
            self.assertEqual(result['model_calls'], 0)
            self.assertFalse(result['campaign_eligible'])

    def test_private_rule_uses_real_observations_and_catches_a_deliberate_defect(self):
        from scripts.ci.browser_quality_fixtures import prepare
        from obench.repair_oracles.activity_explorer import cases
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            controls = prepare(root / 'controls')
            files = fixture()
            selected = [c for c in cases() if c[0] in ('list', 'detail')]
            self.assertEqual(len(selected), 2)
            files['cases.json'] = json.dumps([{'id': n, 'bucket': b, 'request': r} for n, b, r in selected]).encode()
            files['evaluator.py'] = b'''from obench.repair_oracles.activity_explorer_v2 import reasons
def grade(cases, observations, fixtures):
    result = []
    for case, observed in zip(cases, observations, strict=True):
        failures = reasons(case['id'], observed.get('value', {}), case['request'])
        result.append({'pass': not failures, 'reasons': failures})
    return result
'''
            archive = root / 'eval.tar'
            sha = _write(files, {n: 0o644 for n in files}, archive, 'evaluation', {})
            reports = [package.replay(archive, sha, controls / name, os.environ['OBENCH_BROWSER_IMAGE'],
                                     trust_evaluator=True) for name in ('valid-list', 'reversed-list')]
            self.assertEqual(reports[0]['score'], 1)
            self.assertLess(reports[1]['score'], 1)
            self.assertTrue(all(not r['campaign_eligible'] and r['worker']['network'] == 'none' for r in reports))
            self.assertEqual(reports[0]['package_sha256'], sha)
