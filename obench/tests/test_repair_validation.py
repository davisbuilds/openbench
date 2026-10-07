"""Operator evidence contracts; candidate execution is covered by Docker controls."""
import copy
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from obench import repair_validation as rv
from obench.repair_oracles import dojo_v6


class QualityControlsTests(unittest.TestCase):
    def setUp(self):
        self.info = {'checks': [{'id': 'migration', 'bucket': 'migration'},
                                {'id': 'identity', 'bucket': 'identity'}],
                     'buckets': ['migration', 'identity'], 'quality_eligible': True,
                     'revision': {'oracle': 'agentmonitor-benchmark'}}
        def record(name, role, failed):
            return {'id': name, 'role': role, 'must_fail': failed,
                    'result': {'source_sha256': {'source': name}, 'solved': not failed,
                               'grading': {'score': 0 if failed else 1,
                                           'checks': [{'id': c['id'], 'bucket': c['bucket'], 'pass': c['id'] not in failed}
                                                      for c in self.info['checks']]}}}
        self.records = [record('base', 'baseline', ['migration', 'identity']),
                        record('reference', 'valid', []), record('alternative', 'valid', []),
                        record('partial-migration', 'invalid', ['migration']),
                        record('partial-identity', 'invalid', ['identity'])]

    def test_accepts_declared_polarity_alternatives_and_targeted_defects(self):
        self.assertEqual(rv.assess_controls(self.records, self.info), [])

    def test_duplicate_alternatives_and_unrelated_failure_cannot_admit_task(self):
        self.records[2]['result']['source_sha256'] = self.records[1]['result']['source_sha256']
        self.assertTrue(any('duplicate' in s for s in rv.assess_controls(self.records, self.info)))
        self.records[2]['result']['source_sha256'] = {'source': 'other'}
        self.records[-1]['result']['grading']['checks'][1]['pass'] = True
        self.records[-1]['result']['solved'] = True
        self.records[-1]['result']['grading']['score'] = 1
        self.assertTrue(any('escaped' in s for s in rv.assess_controls(self.records, self.info)))

    def test_infra_or_invalid_artifact_is_not_a_successful_negative_control(self):
        self.records[-1]['result']['grading']['candidate_failure'] = 'missing_source'
        self.assertTrue(any('invalid candidate' in s for s in rv.assess_controls(self.records, self.info)))

    def test_wholesale_failure_is_not_targeted_defect_evidence(self):
        for check in self.records[-1]['result']['grading']['checks']:
            check['pass'] = False
        self.assertTrue(any('passing invariant' in s for s in rv.assess_controls(self.records, self.info)))

    def test_malformed_observation_cannot_be_a_passing_reference(self):
        self.records[1]['result']['grading']['checks'][0]['pass'] = 1
        self.assertTrue(rv.assess_controls(self.records, self.info))

    def test_reported_solved_flag_cannot_disagree_with_score(self):
        self.records[1]['result']['grading']['score'] = 0
        self.assertTrue(rv.assess_controls(self.records, self.info))

    def test_no_references_and_missing_bucket_coverage_are_findings(self):
        findings = rv.assess_controls([self.records[0], self.records[3]], self.info)
        self.assertTrue(any('two distinct' in s for s in findings))
        self.assertTrue(any('every scoring bucket' in s for s in findings))


class DojoV6Tests(unittest.TestCase):
    def test_named_contract_fixtures_use_realistic_paths_and_distinguish_sources(self):
        cases = dojo_v6.cases()
        self.assertEqual(len({c[0] for c in cases}), len(cases))
        fixture = next(c for c in cases if c[0] == 'alias-version-equivalence')
        self.assertIn('/1.0.0/skills/review/', fixture[2]['live'])
        self.assertIsNone(fixture[3])
        for name in ('different-skill-file', 'nonplugin-version-directory'):
            self.assertIsInstance(next(c[3] for c in cases if c[0] == name), tuple)
        self.assertNotIn('expected', json.dumps([c[2] for c in cases]))

    def test_observed_expected_and_stable_id_survive_failure(self):
        result = dojo_v6.grade([{'ok': False} for _ in dojo_v6.cases()])
        self.assertEqual(result['score'], 0)
        self.assertEqual(result['checks'][0], {'id': 'native-listing', 'bucket': 'rollout',
                         'pass': False, 'observed': {'ok': False},
                         'expected': {'names': ['actual'], 'surface': 'codex-tui'}})


class CLIContractsTests(unittest.TestCase):
    def test_failed_workflow_chain_cannot_be_hidden_by_later_probe_marker(self):
        command = 'false && printf should-not-run'
        result = subprocess.run(['bash', '-c', 'set -eu\n' + rv.workflow_command(command) + '\nprintf MARKER'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('MARKER', result.stdout)

    def test_help_is_discoverable_and_missing_task_is_incomplete_json(self):
        help_result = subprocess.run([sys.executable, '-m', 'obench', 'repair', '--help'], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('replay', help_result.stdout)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rv.main(['inspect', '/nonexistent/openbench-task', '--json'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'incomplete')

    def test_output_refuses_overwrite_before_any_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'evidence.json'
            p.write_text('original')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rv.main(['replay', tmp, '--source', tmp, '--image', 'sha256:' + 'a' * 64,
                                '--output', str(p), '--json'])
            self.assertEqual(code, 2)
            self.assertEqual(p.read_text(), 'original')

    def test_workflow_requires_exact_task_image_command_and_actual_tool_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'receipt.json'
            p.write_text('[]')
            with self.assertRaisesRegex(ValueError, 'object'):
                rv.validate_workflow(p, {}, 'sha256:' + 'a'*64, 'pnpm build')
            p.write_text(json.dumps({'status': 'passed', 'task': '/different-task'}))
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                rv.validate_workflow(p, {'task': tmp, 'task_binding': {}}, 'sha256:' + 'a'*64, 'pnpm build')

    def test_workflow_detects_changed_underlying_tool_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            info = {'task': tmp, 'task_binding': {'scheme': 3, 'sha256': 'a'*64}}
            report = {'status': 'passed', 'cleanup_confirmed': True, 'live_inference': False,
                      'real_credentials': False, 'actual_tool_mutation': True, 'tool_result_returned': True,
                      'final_response_present': True, 'developer_workflows_passed': True,
                      'task': tmp, 'task_binding': info['task_binding'], 'runtime_image': 'sha256:'+'b'*64,
                      'project_check': 'pnpm build', 'validation_sha256': rv.sha(Path(rv.__file__)), 'probe_sha256': rv.sha(rv.ROOT/'scripts/local/verify_repair_codex.py')}
            files = ('agent/codex.txt', 'agent/developer-workflow.json', 'requests.jsonl', 'gateway.jsonl')
            for name in files:
                p = root/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(name)
            report['evidence_sha256'] = {name: rv.sha(root/name) for name in files}
            receipt = root/'receipt.json'; receipt.write_text(json.dumps(report))
            rv.validate_workflow(receipt, info, report['runtime_image'], 'pnpm build')
            (root/'agent/codex.txt').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'evidence changed'):
                rv.validate_workflow(receipt, info, report['runtime_image'], 'pnpm build')


class ReceiptInputTests(unittest.TestCase):
    def test_loaded_validation_cannot_be_relabelled_with_changed_disk_bytes(self):
        # Read real files from a temporary copy; only relocate the source root.
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in rv.implementation():
                target = root/'obench'/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(rv.ROOT/'obench'/relative, target)
            with patch.object(rv, 'ROOT', root):
                rv.implementation()
                with (root/'obench/repair_validation.py').open('a') as f:
                    f.write('\n# changed policy\n')
                with self.assertRaisesRegex(ValueError, 'changed after import'):
                    rv.implementation()

    def test_malformed_campaign_receipt_is_an_actionable_input_error(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp)/'receipt.json'
            for value in ({}, [], {'task': None}, {'task': {'task': 123}}):
                receipt.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'malformed'):
                    rv.validate_campaign(SimpleNamespace(task_sets=[]), [receipt])


class ReceiptVerdictTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        fixture = QualityControlsTests()
        fixture.setUp()
        self.records = fixture.records
        self.info = {**fixture.info, 'task': str(self.root/'task'),
                     'task_binding': {}, 'source_prefix': 'src'}
        self.image = 'sha256:' + 'b'*64
        self.controls = self.root/'controls.json'
        self.workflow = self.root/'workflow.json'
        self.receipt = self.root/'quality.json'
        specs = []
        self.results = {}
        for record in self.records:
            source = self.root/record['id']
            if record['role'] == 'baseline':
                source = self.root/'task/environment/app'
            (source/'src').mkdir(parents=True)
            (source/'src/example.ts').write_text(record['id'])
            result = {**record['result'], 'task': self.info, 'image': self.image,
                      'implementation': rv.implementation(),
                      'source_sha256': rv.source_hashes(source, self.info)}
            self.results[str(source)] = result
            specs.append({k: record[k] for k in ('id', 'role', 'must_fail')} | {'source': str(source)})
        self.controls.write_text(json.dumps({'schema': 1, 'contract_review': 'reviewed controls',
                                             'project_check': 'true', 'controls': specs}))
        evidence = {}
        for name in ('agent/codex.txt', 'agent/developer-workflow.json', 'requests.jsonl', 'gateway.jsonl'):
            file = self.root/name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(name)
            evidence[name] = rv.sha(file)
        self.workflow.write_text(json.dumps({
            'status': 'passed', 'cleanup_confirmed': True, 'live_inference': False,
            'real_credentials': False, 'actual_tool_mutation': True, 'tool_result_returned': True,
            'final_response_present': True, 'developer_workflows_passed': True,
            'runtime_image': self.image, 'task': self.info['task'], 'task_binding': {},
            'project_check': 'true', 'probe_sha256': rv.sha(rv.ROOT/'scripts/local/verify_repair_codex.py'),
            'validation_sha256': rv.sha(Path(rv.__file__)), 'evidence_sha256': evidence}))
        # Isolate only task discovery and external Docker grading. Specification,
        # source/evidence files, hashing, control assessment and receipt parsing
        # use the production paths. Real Docker controls cover both task families.
        self.inspect = patch.object(rv, 'inspect_task', return_value=self.info)
        self.inspect.start(); self.addCleanup(self.inspect.stop)
        self.grader = patch.object(rv, 'replay', side_effect=lambda task, source, image:
                                  copy.deepcopy(self.results[str(source)]))
        self.replay = self.grader.start(); self.addCleanup(self.grader.stop)

    def produce(self):
        return rv.validate(self.info['task'], self.controls, self.image, self.workflow)

    def admit(self, receipt):
        self.receipt.write_text(json.dumps(receipt))
        return rv.validate_receipt(self.receipt, self.info['task'], self.image)

    def test_untampered_receipt_recomputes_all_control_results(self):
        receipt = self.produce()
        self.replay.reset_mock()
        self.admit(receipt)
        self.assertEqual(self.replay.call_count, len(self.records))

    def test_admission_returns_fresh_observations_instead_of_saved_claims(self):
        receipt = self.produce()
        receipt['controls'][1]['result']['grading']['checks'][0]['observed'] = {'forged': True}
        self.results[str(self.root/'reference')]['grading']['checks'][0]['observed'] = {'worker_path': '/tmp/fresh-worker'}
        admitted = self.admit(receipt)
        self.assertEqual(admitted['controls'][1]['result']['grading']['checks'][0]['observed'],
                         {'worker_path': '/tmp/fresh-worker'})

    def test_failed_reference_cannot_be_promoted_by_editing_saved_verdicts(self):
        result = self.results[str(self.root/'reference')]
        result['solved'] = False
        result['grading']['score'] = 0
        result['grading']['checks'][0]['pass'] = False
        receipt = self.produce()
        self.assertEqual(receipt['status'], 'failed')
        receipt['status'] = 'passed'
        receipt['findings'] = []
        stored = receipt['controls'][1]['result']
        stored['solved'] = True
        stored['grading']['score'] = 1
        stored['grading']['checks'][0]['pass'] = True
        self.assertEqual(rv.assess_controls(receipt['controls'], self.info), [])
        with self.assertRaisesRegex(ValueError, 'recomputed'):
            self.admit(receipt)

    def test_control_targets_cannot_be_edited_independently_of_specification(self):
        receipt = self.produce()
        receipt['controls'][0]['must_fail'] = ['identity']
        self.assertEqual(rv.assess_controls(receipt['controls'], self.info), [])
        with self.assertRaisesRegex(ValueError, 'specification'):
            self.admit(receipt)
