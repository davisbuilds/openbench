"""Reporting rejects corruption and incompatible evidence before aggregation."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

from obench import results_query as query, report
from obench.harbor_job import canonical_comparison_plan_bytes
from obench.tests.test_compare import CompareTestCase


class ResultsIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, rows):
        path = self.root / name
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return str(path)

    def run_query(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            query.main(list(args))
        return out.getvalue()

    def legacy(self, **extra):
        return dict(harness='codex', model='m', task='task', trial=1,
                    run_id='attempt-1', success=True, failure_class='solved', **extra)

    def harbor(self, **extra):
        return CompareTestCase.harbor_row('codex', 'task', 1, True, arm_id='codex', **extra)

    def test_missing_files_keep_user_facing_behavior(self):
        path = str(self.root / 'missing.jsonl')
        self.assertEqual(report.load_rows(path), [])
        with self.assertRaisesRegex(SystemExit, 'no such results file'):
            query.load([path])

    def test_missing_entire_harbor_arm_is_explicit(self):
        path = self.write('absent-arm.jsonl', [self.harbor()])
        out = self.run_query('summary', path)
        self.assertIn('MISSING-ARM pi x m [pi]: 0/1 planned cells', out)
        self.assertNotIn('MISSING-ARM codex', out)
        filtered = self.run_query('summary', path, '--harness', 'codex')
        self.assertNotIn('MISSING-ARM', filtered)

    def test_corruption_has_file_and_line_in_both_readers(self):
        path = self.write('bad.jsonl', [self.legacy()])
        with open(path, 'a') as stream:
            stream.write('not json\n')
        for reader in (query.load, lambda paths: report.load_rows(paths[0])):
            with self.subTest(reader=reader), self.assertRaisesRegex(ValueError, 'bad.jsonl:2'):
                reader([path])

    def test_non_objects_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        path = self.root / 'invalid.jsonl'
        for text in ('[]', 'null', '{"score":NaN}', '{"score":1e999}', '{"task":"a","task":"b"}'):
            path.write_text(text + '\n')
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'invalid.jsonl:1'):
                query.load([str(path)])

    def test_valid_file_and_same_attempt_retry_remain_supported(self):
        a = self.legacy()
        b = {**a, 'failure_class': 'infra', 'success': False}
        out = self.run_query('summary', self.write('valid.jsonl', [b, a]))
        self.assertRegex(out, r'codex x m\s+1\s+1\s+1\s+100%')

    def test_task_digest_drift_is_rejected_even_across_arms(self):
        a = self.legacy(task_content_digest={'scheme': 3, 'sha256': 'a'*64})
        b = {**a, 'model': 'other', 'task_content_digest': {'scheme': 3, 'sha256': 'b'*64}}
        with self.assertRaisesRegex(ValueError, 'task.*identity'):
            self.run_query('summary', self.write('mixed.jsonl', [a,b]))

    def test_separate_studies_cannot_overwrite_the_same_cell(self):
        a = self.legacy(study_sha256='a'*64)
        b = {**a, 'study_sha256': 'b'*64}
        with self.assertRaisesRegex(ValueError, 'study'):
            self.run_query('summary', self.write('studies.jsonl', [a,b]))

    def test_distinct_run_ids_cannot_silently_overwrite_a_legacy_cell(self):
        a = self.legacy()
        with self.assertRaisesRegex(ValueError, 'run identity'):
            self.run_query('summary', self.write('runs.jsonl', [a,{**a,'run_id':'attempt-2'}]))

    def test_harbor_valid_plan_and_cross_plan_refusal(self):
        a = self.harbor()
        b = CompareTestCase.harbor_row('pi','task',1,False,arm_id='pi')
        valid = self.write('valid.jsonl', [a,b])
        self.assertIn('matched cells (every arm has a verdict): 1', self.run_query('matched',valid))
        changed = self.harbor(plan_variant='-different')
        with self.assertRaisesRegex(ValueError, 'comparison-plan'):
            self.run_query('summary', self.write('mixed.jsonl',[a,changed]))

    def test_harbor_plan_counts_unobserved_attempts(self):
        row = self.harbor()
        p = row['candidate_provenance']
        p['comparison_plan']['attempts'] = 2
        p['comparison_plan_sha256'] = hashlib.sha256(canonical_comparison_plan_bytes(p['comparison_plan'])).hexdigest()
        out = self.run_query('summary', self.write('partial.jsonl',[row]))
        self.assertRegex(out, r'codex x m \[codex\]\s+1\s+1\s+2\s+100%\s+50%')

    def test_same_labels_different_profiles_do_not_collapse(self):
        rows = [CompareTestCase.harbor_row('acme','task',1,True,arm_id=arm, model='model-x') for arm in ('strict','fast')]
        out = self.run_query('summary', self.write('profiles.jsonl', rows))
        self.assertIn('acme x model-x [strict]',out)
        self.assertIn('acme x model-x [fast]',out)

    def test_separate_input_reports_never_pool_different_treatments(self):
        a = self.write('one.jsonl', [self.harbor()])
        b = self.write('two.jsonl', [self.harbor(plan_variant='-different')])
        out = self.run_query('summary', a,b,'--separate-inputs')
        self.assertIn(a,out)
        self.assertIn(b,out)
        self.assertEqual(sum(line.startswith('codex x m [codex]') for line in out.splitlines()),2)

    def test_sealed_suite_validates_before_model_filter(self):
        from obench import init, suite_run
        from obench.tests.test_suite_run import SuiteRunTests
        init.init_scaffold(self.root)
        suite = self.root / '.openbench/suites/default.toml'
        suite.write_text(suite.read_text().replace('attempts = 1', 'attempts = 2'))
        compiled = suite_run.compile_suite(suite)
        rows = []
        for job in suite_run.plan_jobs(compiled):
            path = self.root / (job.task_set_id + '.plan.json')
            path.write_bytes(canonical_comparison_plan_bytes(job.artifact.comparison_plan.as_dict()))
            rows.extend(suite_run._bind_suite_provenance(compiled, job.task_set_id,
                        SuiteRunTests._simulated_rows(None, path)))
        path = self.write('sealed.jsonl', rows)
        self.assertIn('evidence:', self.run_query('summary', path, '--model', rows[0]['model']))
        self.assertGreater(len(rows), 1)
        with self.assertRaisesRegex(ValueError, 'incomplete|missing'):
            self.run_query('summary', self.write('missing.jsonl',rows[:-1]), '--model', rows[0]['model'])

    def test_duplicate_harbor_attempt_is_rejected(self):
        row = self.harbor()
        with self.assertRaisesRegex(ValueError, 'duplicate Harbor'):
            self.run_query('summary', self.write('duplicate.jsonl', [row,row]))

    def test_evidence_remains_available_for_mixed_valid_json(self):
        path = self.write('diagnostic.jsonl',[self.harbor(), self.harbor(plan_variant='-different')])
        self.assertIn(path,self.run_query('evidence',path))
