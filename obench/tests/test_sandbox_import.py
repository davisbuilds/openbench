"""Synthetic whole-job import controls; no Harbor execution or model calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from obench.harbor_results import HarborResultsError, import_results
from obench.tests.test_harbor_results import GoldenHarborJob, _comparison_arm, _write_json

IMAGE = 'sha256:' + 'a' * 64
AGENT = {
    'import_path': 'obench.harbor_agents.sandbox_codex:SandboxCodex',
    'model_name': 'gpt-5.6-terra',
    'kwargs': {'version': '0.154.0', 'reasoning_effort': 'xhigh'},
}


def edit_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    _write_json(path, value)


class SandboxImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, *, rejected=False):
        score = 0.0 if rejected else 1.0
        fixture = GoldenHarborJob(self.root / 'job', specs=[{
            'name': 'dojo__one', 'task': 'dojo-evidence-pr60-v3',
            'id': '00000000-0000-0000-0000-000000000001',
            'score': score, 'offset': 0,
        }])
        fixture.set_custom_agent(0, AGENT, reported_name='codex')
        trial = fixture.trial()
        lock = json.loads((trial / 'lock.json').read_text())
        lock['environment'].pop('type')
        lock['environment']['import_path'] = 'obench.harbor_sandbox:RepairSandbox'
        lock['environment']['kwargs'] = {'runtime_image': IMAGE}
        lock['verifier']['import_path'] = 'obench.sandbox_grading:RepairVerifier'
        lock['verifier']['kwargs'] = {'worker_image': IMAGE}
        _write_json(trial / 'lock.json', lock)
        edit_json(fixture.root / 'lock.json', lambda value: value.update(trials=[lock]))
        result = json.loads((trial / 'result.json').read_text())
        result['agent_info']['version'] = '0.154.0'
        result['config']['environment'] = lock['environment']
        result['config']['verifier'] = lock['verifier']
        _write_json(trial / 'result.json', result)
        edit_json(trial / 'agent/trajectory.json',
                  lambda value: value['agent'].update(version='0.154.0'))
        binding = {
            'scheme': 3, 'schema': 'openbench-isolated-repair-task-v1',
            'task_config': {'task': {'name': 'openbench/dojo-evidence-pr60-v3'}},
            'task_files_sha256': {'instruction.md': '1' * 64},
            'grading_module_sha256': '2' * 64, 'worker_entry_sha256': '3' * 64,
        }
        digest = {'scheme': 3, 'sha256': hashlib.sha256(json.dumps(
            binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        edit_json(trial / 'verifier/openbench-verifier-evidence.json',
                  lambda value: value.update(openbench_task_content_digest=digest))
        ledger = (b'{"event":"ready","role":"broker"}\n'
                  b'{"event":"request","model":"gpt-5.6-terra","effort":"xhigh"}\n'
                  b'{"event":"stopped","role":"broker","clean":true}\n')
        (trial / 'verifier/sandbox-gateway.jsonl').write_bytes(ledger)
        source_hash = hashlib.sha256((trial / 'artifacts/workspace/answer.txt').read_bytes()).hexdigest()
        receipt = {
            'task_binding': binding,
            'freeze': {'solver_stopped': True, 'broker_revoked': True,
                       'gateway_ledger_sha256': hashlib.sha256(ledger).hexdigest(),
                       'gateway_module_sha256': '4' * 64, 'image_id': IMAGE,
                       'files': {'answer.txt': {'sha256': source_hash}}},
            'grading': {'score': score, 'source_sha256': {'answer.txt': source_hash},
                        'worker': {'network': 'none', 'user': '10001:10001',
                                   'host_mounts': False, 'read_only_root': True,
                                   'capabilities': 'none', 'requested_image': IMAGE,
                                   'image_id': IMAGE}},
        }
        if rejected:
            receipt['freeze'].pop('files')
            receipt['grading'].update(candidate_failure='invalid_source_artifact',
                                      source_sha256=None, worker=None)
            (trial / 'artifacts/workspace/answer.txt').unlink()
            edit_json(trial / 'artifacts/manifest.json',
                      lambda value: value[1].update(status='failed'))
        _write_json(trial / 'verifier/sandbox-grading.json', receipt)
        fixture.sync_aggregate()
        plan = fixture.write_comparison_plan(
            attempts=1, arms=[_comparison_arm('isolated', AGENT, canonical_harness='codex')],
            agents=[AGENT])
        return fixture, plan

    def import_fixture(self, fixture, plan):
        return import_results(
            fixture.root, self.root / 'rows.jsonl', comparison_plan_path=plan,
            submitted_job_config_path=fixture.submitted_config_path)

    def test_valid_score_one_import_binds_grading_receipt(self):
        fixture, plan = self.fixture()
        rows = self.import_fixture(fixture, plan)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['score'], 1.0)
        receipt = fixture.trial() / 'verifier/sandbox-grading.json'
        self.assertEqual(rows[0]['candidate_provenance']['sandbox_grading_sha256'],
                         hashlib.sha256(receipt.read_bytes()).hexdigest())
        self.assertEqual(rows[0]['candidate_provenance']['sandbox_gateway_module_sha256'], '4' * 64)
        self.assertEqual(len((self.root / 'rows.jsonl').read_text().splitlines()), 1)

    def test_rejected_source_imports_as_wrong_answer_without_workspace(self):
        fixture, plan = self.fixture(rejected=True)
        row = self.import_fixture(fixture, plan)[0]
        self.assertEqual(row['score'], 0.0)
        self.assertEqual(row['failure_class'], 'wrong_answer')
        self.assertIsNone(row['workspace_source'])
        self.assertIsNone(row['candidate_provenance']['final_workspace_sha256'])

    def test_receipt_and_ledger_tampering_reject_before_output(self):
        mutations = {
            'solver_running': lambda value: value['freeze'].update(solver_stopped=False),
            'missing_gateway_hash': lambda value: value['freeze'].update(gateway_module_sha256=None),
            'oracle_change': lambda value: value['task_binding'].update(grading_module_sha256='f' * 64),
            'source_change': lambda value: value['grading']['source_sha256'].update({'answer.txt': 'f' * 64}),
            'image_change': lambda value: value['grading']['worker'].update(image_id='sha256:' + 'b' * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                fixture, plan = self.fixture()
                edit_json(fixture.trial() / 'verifier/sandbox-grading.json', mutate)
                with self.assertRaises(HarborResultsError):
                    self.import_fixture(fixture, plan)
                self.assertFalse((self.root / 'rows.jsonl').exists())
        with tempfile.TemporaryDirectory() as directory:
            self.root = Path(directory)
            fixture, plan = self.fixture()
            ledger = fixture.trial() / 'verifier/sandbox-gateway.jsonl'
            ledger.write_bytes(ledger.read_bytes() + b'{}\n')
            with self.assertRaisesRegex(HarborResultsError, 'ledger'):
                self.import_fixture(fixture, plan)
            self.assertFalse((self.root / 'rows.jsonl').exists())

    def test_rehashed_gateway_treatment_drift_is_rejected(self):
        for field, replacement in [('model', 'gpt-5.6-luna'), ('effort', 'low')]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                fixture, plan = self.fixture()
                ledger = fixture.trial() / 'verifier/sandbox-gateway.jsonl'
                records = [json.loads(line) for line in ledger.read_text().splitlines()]
                records[1][field] = replacement
                ledger.write_text(''.join(json.dumps(record) + '\n' for record in records))
                edit_json(fixture.trial() / 'verifier/sandbox-grading.json',
                          lambda value: value['freeze'].update(
                              gateway_ledger_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest()))
                with self.assertRaisesRegex(HarborResultsError, 'treatment differs'):
                    self.import_fixture(fixture, plan)
                self.assertFalse((self.root / 'rows.jsonl').exists())

    def test_cli_version_and_invalid_rejection_exception_remain_fail_closed(self):
        fixture, plan = self.fixture(rejected=True)
        edit_json(fixture.trial() / 'result.json',
                  lambda value: value['agent_info'].update(version='0.153.0'))
        with self.assertRaisesRegex(HarborResultsError, 'pinned sandbox CLI'):
            self.import_fixture(fixture, plan)
        edit_json(fixture.trial() / 'result.json',
                  lambda value: value['agent_info'].update(version='0.154.0'))
        edit_json(fixture.trial() / 'verifier/sandbox-grading.json',
                  lambda value: value['grading'].update(candidate_failure='worker timeout'))
        with self.assertRaisesRegex(HarborResultsError, "status='ok'"):
            self.import_fixture(fixture, plan)
        self.assertFalse((self.root / 'rows.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
