"""Admission binding and control construction without provider requests."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from obench import init, runtime_admission as admission, suite_run
from obench.campaign import write_record
from obench.tests import test_suite_run


class RuntimeAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        init.init_scaffold(self.root)
        tasks=self.root/'.openbench/tasks'
        shutil.rmtree(tasks)
        source=Path(__file__).resolve().parents[2]/'harbor-tasks-local/dojo-evidence-pr60-v4'
        shutil.copytree(source,tasks/source.name)
        suite=self.root/'.openbench/suites/default.toml'
        suite.write_text(suite.read_text().replace('gpt-5.6-sol','gpt-5.6-terra-xhigh')+'\n[sandbox]\nkind="repair-v1"\nruntime_image="sha256:'+'a'*64+'"\n')
        self.compiled=suite_run.compile_suite(suite)
        self.control,self.task=admission.prepare_control(self.compiled,self.root/'control')

    def test_control_is_separate_bounded_and_resealed(self):
        self.assertNotEqual(self.control.manifest_sha256,self.compiled.manifest_sha256)
        self.assertEqual(self.control.suite.run.timeout_seconds,180)
        self.assertEqual(self.control.suite.run.max_retries,0)
        self.assertEqual(self.control.suite.sandbox.max_requests,20)
        self.assertIn(admission.MARKER.decode().strip(),(self.task/'instruction.md').read_text())
        self.assertEqual(suite_run.compile_suite(self.compiled.suite.path).manifest_sha256,self.compiled.manifest_sha256)

    def test_missing_admission_refuses_campaign_before_runtime_or_auth(self):
        from obench import campaign
        with patch.object(campaign, 'git_identity', return_value={'root':str(self.root),'commit':'test'}), patch.object(campaign.shutil,'which',return_value='/usr/bin/python3'):
            with self.assertRaisesRegex(campaign.CampaignError, 'require --admission'):
                campaign.launch_campaign(self.compiled.suite.path)
        self.assertFalse((Path(self.compiled.config.results_dir)/'campaigns').exists())

    def receipt(self):
        fixture=test_suite_run.SuiteRunTests()
        def imported(job_path,*,comparison_plan_path,submitted_job_config_path):
            rows=fixture._simulated_rows(Path(comparison_plan_path))
            for row in rows:
                row['candidate_provenance'].update(sandbox_grading_sha256='c'*64,
                    sandbox_gateway_module_sha256=self.control.manifest['sandbox']['implementation_sha256']['obench.sandbox_gateway'])
            return rows
        # Only the external Harbor interpreter/process is simulated. The suite,
        # plan, rows, result files and manifest sealing/verification are real.
        with patch.object(suite_run,'_verify_sandbox_runtime'):
            result=fixture._run_simulated(self.control,importer=imported)
        image=self.control.suite.sandbox.runtime_image
        expected={'image':{'requested':image},'implementation':{admission.SCRIPTS[0]:'b'*64},
                  'models':[['gpt-5.6-terra-xhigh','gpt-5.6-terra','xhigh']]}
        (self.root/'boundary.json').write_text(json.dumps({'runtime_image':image,'probe_sha256':'b'*64}))
        for i in range(len(admission.CONTROLS)):
            (self.root/f'offline-{i}.log').write_text('synthetic offline control\n')
        files=[p for p in self.root.rglob('*') if p.is_file()]
        value={'schema':1,'kind':'repair-runtime-admission','passed':True,'fingerprint':expected,
               'offline_controls':{script:{'exit_code':0,'log':f'offline-{i}.log'} for i,script in enumerate(admission.CONTROLS)},
               'authenticated_control':str(result.run_manifest_path.relative_to(self.root)),
               'evidence':{str(p.relative_to(self.root)):admission.digest(p) for p in files}}
        path=self.root/'admission.json'
        write_record(path,value)
        return path,expected,value

    def test_matching_evidence_is_accepted_and_changed_runtime_refused(self):
        path,expected,value=self.receipt()
        self.assertTrue(admission.validate_admission(path,expected)['passed'])
        changed=copy.deepcopy(expected)
        changed['image']['requested']='sha256:'+'d'*64
        with self.assertRaisesRegex(admission.AdmissionError,'stale'):
            admission.validate_admission(path,changed)

    def test_missing_or_modified_control_evidence_is_refused(self):
        path,expected,_=self.receipt()
        (self.root/'offline-0.log').write_text('modified')
        with self.assertRaisesRegex(admission.AdmissionError,'evidence changed'):
            admission.validate_admission(path,expected)
        (self.root/'offline-0.log').unlink()
        with self.assertRaisesRegex(admission.AdmissionError,'evidence path'):
            admission.validate_admission(path,expected)

    def test_unbound_authenticated_control_and_missing_offline_control_are_refused(self):
        path,expected,value=self.receipt()
        for key,replacement in [('authenticated_control','../other.run.json'),('offline_controls',{})]:
            bad={**value,key:replacement}
            write_record(path,bad)
            with self.subTest(key=key),self.assertRaises(admission.AdmissionError):
                admission.validate_admission(path,expected)

    def test_wrong_model_control_cannot_admit_requested_model(self):
        path,expected,value=self.receipt()
        changed=copy.deepcopy(expected)
        changed['models']=[['gpt-5.6-luna-max','gpt-5.6-luna','max']]
        value['fingerprint']=changed
        write_record(path,value)
        with self.assertRaisesRegex(admission.AdmissionError,'another execution treatment'):
            admission.validate_admission(path,changed)

    def test_file_edit_control_rejects_all_unrequested_workspace_changes(self):
        from obench.harbor_sandbox import read_tree, source_receipt
        path,_,value=self.receipt()
        verified=suite_run.verify_suite_run(self.root/value['authenticated_control'])
        result_path=Path(verified['results_path'])
        rows=[json.loads(line) for line in result_path.read_text().splitlines()]
        jobs={j.task_set_id:Path(self.control.config.jobs_dir)/j.artifact.job_name for j in suite_run.plan_jobs(self.control)}
        for row in rows:
            row['candidate_provenance']['harbor_trial_name']='control-trial'
        result_path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        trial=jobs[rows[0]['candidate_provenance']['suite_task_set_id']]/'control-trial'
        (trial/'verifier').mkdir(parents=True)
        (trial/'verifier/sandbox-gateway.jsonl').write_text(json.dumps({'event':'request','outcome':'complete','upstream_status':200,'upstream_peer':{'ip':'8.8.8.8','port':443}})+'\n')
        expected=read_tree(self.task/'environment/app')
        expected[admission.CONTROL_TARGET]+=admission.MARKER
        from obench.harbor_sandbox import write_files
        write_files(trial/'artifacts/workspace', {name:data for name,data in expected.items() if name.startswith('scripts/profiles/')})
        receipt=trial/'verifier/sandbox-grading.json'
        receipt.write_text(json.dumps({'freeze':{'workspace_files':source_receipt(expected)['files']}}))
        admission.verify_control(self.control,self.task,result_path)
        for change in ('requirements','extra-file','missing-file','missing-marker'):
            files=dict(expected)
            if change=='requirements': files['requirements.txt']=b'changed dependency\n'
            elif change=='extra-file': files['new.txt']=b'unrequested\n'
            elif change=='missing-file': del files['requirements.txt']
            else: files[admission.CONTROL_TARGET]=files[admission.CONTROL_TARGET].removesuffix(admission.MARKER)
            receipt.write_text(json.dumps({'freeze':{'workspace_files':source_receipt(files)['files']}}))
            with self.subTest(change=change),self.assertRaisesRegex(admission.AdmissionError,'beyond the requested edit'):
                admission.verify_control(self.control,self.task,result_path)

    def test_qualified_status_requires_intact_admission_evidence(self):
        import socket
        from obench import campaign
        path,_,value=self.receipt()
        write_record(self.root/'launch.json',{'schema':1,'host':socket.gethostname(),'mode':'qualify',
            'session':'not-running','manifest_sha256':self.compiled.manifest_sha256,'jobs':[]})
        write_record(self.root/'finished.json',{'schema':1,'state':'qualified','admission':str(path),'exit_code':0})
        (self.root/'execution.lock').touch()
        with patch.object(campaign,'session_exists',return_value=False):
            self.assertEqual(campaign.campaign_status(self.root)['state'],'qualified')
            log=self.root/'offline-0.log'
            original=log.read_bytes()
            log.write_text('changed')
            self.assertEqual(campaign.campaign_status(self.root)['state'],'completion_evidence_invalid')
            log.write_bytes(original)
            self.assertEqual(campaign.campaign_status(self.root)['state'],'qualified')
            path.unlink()
            self.assertEqual(campaign.campaign_status(self.root)['state'],'completion_evidence_invalid')
