"""Behavioral guards for the developing concurrency oracle; no candidate execution."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from experiments.repair_cases.am123 import driver, oracle


class Am123OracleTests(unittest.TestCase):
    def test_all_seed_scripts_are_valid_and_old_fixture_is_actually_old(self):
        for name,case in oracle.cases():
            with self.subTest(case=name):
                db=sqlite3.connect(':memory:')
                try:
                    db.executescript(case['sql'])
                    columns={r[1] for r in db.execute('PRAGMA table_info(browsing_sessions)')}
                    self.assertEqual('project_identity' in columns,name not in ('structure','legacy_read'))
                    if name=='foreign_keys':
                        self.assertEqual(len(db.execute('PRAGMA foreign_key_list(trace_quality_export_state)').fetchall()),2)
                finally:
                    db.close()

    def test_fixture_and_source_provenance_bind_actual_bytes(self):
        root=oracle.ROOT
        source=json.loads((root/'SOURCE_MANIFEST.json').read_text())
        for name,record in source['files'].items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),record['sha256'])
        fixture=json.loads((root/'FIXTURE_MANIFEST.json').read_text())
        for name,expected in fixture['files'].items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),expected)

    def example(self,case_name):
        case=dict(oracle.cases())[case_name]
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'fixture.db'
            driver.seed(database,case['sql'])
            state=driver.snapshot(database)
        schema=state.pop('schema')
        state['schema_key']='schema'
        worker={'exit_code':0,'result':{'error':None,'foreign_keys_before':1,'foreign_keys_after':1,'in_transaction':False,'reads':0,'read_value':{'id':'keep','value':'unchanged'}}}
        row={'workers':[worker],'state':state}
        if case_name=='current_read':
            row['writer_held']=True
            observations=[row]
        else:
            row['ordinal']='serial'
            pair={**copy.deepcopy(row),'ordinal':0,'workers':[copy.deepcopy(worker),copy.deepcopy(worker)]}
            pair['reopen']=copy.deepcopy(worker)
            pair['reopened_state']=pair['state']
            observations=[row,pair]
        return {'schemas':{'schema':schema},'observations':observations}

    def test_valid_current_read_and_preservation_failures_are_distinguished(self):
        value=self.example('current_read')
        self.assertTrue(oracle.grade('current_read',value))
        for defect in ('missing-column','version','sentinel','integrity','worker-error','foreign-mode','open-transaction'):
            bad=copy.deepcopy(value)
            row=bad['observations'][0]
            if defect=='missing-column': bad['schemas']['schema']['browsing_sessions'].remove('project_identity')
            elif defect=='version': row['state']['version']=0
            elif defect=='sentinel': row['state']['rows']['preserved']=[]
            elif defect=='integrity': row['state']['integrity']=['damaged']
            elif defect=='worker-error': row['workers'][0]['result']['error']={'code':'SQLITE_BUSY'}
            elif defect=='foreign-mode': row['workers'][0]['result']['foreign_keys_after']=0
            else: row['workers'][0]['result']['in_transaction']=True
            with self.subTest(defect=defect):
                self.assertFalse(oracle.grade('current_read',bad))
        extended=copy.deepcopy(value)
        extended['schemas']['schema']['browsing_sessions'].append('additional_valid_column')
        self.assertTrue(oracle.grade('current_read',extended))

    def test_noop_and_double_correction_are_rejected_but_correct_rows_pass(self):
        value=self.example('correction')
        self.assertFalse(oracle.grade('correction',value))
        for observation in value['observations']:
            row=observation['state']
            row['version']=7
            for event in row['rows']['events']:
                if event['event_id']=='first': event['tokens_in']=60000
                if event['event_id']=='second': event['tokens_in']=80000
        self.assertTrue(oracle.grade('correction',value))
        bad=copy.deepcopy(value)
        next(e for e in bad['observations'][0]['state']['rows']['events'] if e['event_id']=='first')['tokens_in']=20000
        self.assertFalse(oracle.grade('correction',bad))

    def test_reopen_cannot_silently_change_a_previously_valid_state(self):
        value=self.example('current_read')
        row=value['observations'][0]
        row['reopen']=copy.deepcopy(row['workers'][0])
        row['reopened_state']=copy.deepcopy(row['state'])
        self.assertTrue(oracle.grade('current_read',value))
        row['reopened_state']['version']=0
        self.assertFalse(oracle.grade('current_read',value))

    def test_omitted_schedules_and_missing_writer_or_actual_read_fail(self):
        value=self.example('current_read')
        self.assertTrue(oracle.grade('current_read',value))
        for key in ('writer_held','read_value'):
            bad=copy.deepcopy(value)
            row=bad['observations'][0]
            if key=='writer_held': row.pop(key)
            else: row['workers'][0]['result'].pop(key)
            self.assertFalse(oracle.grade('current_read',bad))
        value=self.example('correction')
        for observation in value['observations']:
            state=observation['state']
            state['version']=7
            for event in state['rows']['events']:
                event['tokens_in']={'first':60000,'second':80000,'anthropic':12000}[event['event_id']]
        self.assertTrue(oracle.grade('correction',value))
        self.assertFalse(oracle.grade('correction',{'schemas':value['schemas'],'observations':value['observations'][:1]}))
        for key in ('reopen','reopened_state'):
            bad=copy.deepcopy(value)
            bad['observations'][1].pop(key)
            self.assertFalse(oracle.grade('correction',bad))
        failed=self.example('correction')['observations'][0]
        failed['phase']='injected_failure'
        failed['workers'][0]['result']['error']={'code':'SQLITE_CONSTRAINT_TRIGGER','message':'injected correction failure'}
        retry=copy.deepcopy(value['observations'][0])
        retry['phase']='retry'
        rollback={'schemas':value['schemas'],'observations':[failed,retry]}
        self.assertTrue(oracle.grade('rollback',rollback))
        for row in (failed,retry):
            self.assertFalse(oracle.grade('rollback',{**rollback,'observations':[row]}))
        for defect in ('wrong-error','crashed-worker','open-transaction','foreign-mode'):
            bad=copy.deepcopy(rollback)
            worker=bad['observations'][0]['workers'][0]
            if defect=='wrong-error': worker['result']['error']['message']='unrelated failure'
            elif defect=='crashed-worker': worker['exit_code']=1
            elif defect=='open-transaction': worker['result']['in_transaction']=True
            else: worker['result']['foreign_keys_after']=0
            self.assertFalse(oracle.grade('rollback',bad))
