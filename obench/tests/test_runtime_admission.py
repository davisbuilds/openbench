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
        source=Path(__file__).resolve().parents[2]/'benchmarks/harbor/local/dojo-evidence-pr60-v4'
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
        expected={'image':{'requested':image},'implementation':{admission.SCRIPTS[0]:'b'*64, admission.SCRIPTS[1]:'c'*64},
                  'models':[['gpt-5.6-terra-xhigh','gpt-5.6-terra','xhigh']]}
        (self.root/'boundary.json').write_text(json.dumps({'runtime_image':image,'probe_sha256':'b'*64}))
        for i in range(len(admission.CONTROLS)):
            (self.root/f'offline-{i}.log').write_text('synthetic offline control\n')
        model_records = self.model_receipts(expected)
        files=[p for p in self.root.rglob('*') if p.is_file()]
        value={'schema':1,'kind':'repair-runtime-admission','passed':True,'fingerprint':expected,
               'offline_controls':{script:{'exit_code':0,'log':f'offline-{i}.log'} for i,script in enumerate(admission.CONTROLS)},
               'model_controls':model_records,
               'authenticated_control':str(result.run_manifest_path.relative_to(self.root)),
               'evidence':{str(p.relative_to(self.root)):admission.digest(p) for p in files}}
        path=self.root/'admission.json'
        write_record(path,value)
        return path,expected,value

    def model_receipt(self, expected, alias, record):
        return {'status':'passed', 'cleanup_confirmed':True,
            'live_inference':False, 'real_credentials':False, 'model_alias':alias,
            'model':record['model'], 'effort':record['effort'],
            'runtime_image':expected['image']['requested'], 'codex_version':'0.157.0',
            'probe_sha256':expected['implementation'][admission.SCRIPTS[1]],
            'actual_tool_mutation':True, 'tool_result_returned':True,
            'final_response_present':True, 'request_count':2, 'developer_workflows_passed':True}

    def model_receipts(self, expected):
        records = admission.model_control_records(expected)
        for alias, record in records.items():
            path = self.root / record['receipt']
            path.parent.mkdir(parents=True, exist_ok=True)
            (self.root / record['log']).write_text('synthetic model control\n')
            write_record(path, self.model_receipt(expected, alias, record))
        return records

    def test_each_selected_model_requires_bound_correct_offline_evidence(self):
        expected = {'image':{'requested':'sha256:'+'a'*64},
                    'implementation':{admission.SCRIPTS[1]:'c'*64},
                    'models':[['gpt-6-sol-low','gpt-6-sol','low'],
                              ['gpt-6-luna-max','gpt-6-luna','max']]}
        records = self.model_receipts(expected)
        evidence = {r[k]:'unused' for r in records.values() for k in ('log','receipt')}
        admission.validate_model_controls(self.root, expected, records, evidence)
        captured = {**expected, 'context_sha256': 'd'*64}
        with self.assertRaisesRegex(admission.AdmissionError, 'captured context'):
            admission.validate_model_controls(self.root, captured, records, evidence)
        for record in records.values():
            path = self.root / record['receipt']
            receipt = json.loads(path.read_text())
            receipt['context'] = {'sha256': 'd'*64, 'global_loaded': True}
            write_record(path, receipt)
        admission.validate_model_controls(self.root, captured, records, evidence)
        with self.assertRaisesRegex(admission.AdmissionError, 'every selected model'):
            admission.validate_model_controls(self.root, expected, {}, evidence)
        missing = dict(evidence)
        del missing[records['gpt-6-luna-max']['receipt']]
        with self.assertRaisesRegex(admission.AdmissionError, 'not bound'):
            admission.validate_model_controls(self.root, expected, records, missing)
        path = self.root / records['gpt-6-luna-max']['receipt']
        original = json.loads(path.read_text())
        write_record(path, {**original, 'request_count': 4})
        admission.validate_model_controls(self.root, expected, records, evidence)
        for change in ({'effort':'low'}, {'model':'gpt-5.6-terra'}, {'status':'failed'},
                       {'codex_version':'0.154.0'}, {'actual_tool_mutation':False},
                       {'request_count':1}, {'request_count':9}, {'request_count':True}):
            write_record(path, {**original, **change})
            with self.subTest(change=change), self.assertRaisesRegex(admission.AdmissionError, 'execution treatment'):
                admission.validate_model_controls(self.root, expected, records, evidence)
        write_record(path, None)
        with self.assertRaisesRegex(admission.AdmissionError, 'object'):
            admission.validate_model_controls(self.root, expected, records, evidence)

    def test_browser_profile_requires_image_transport_for_every_model(self):
        expected = {'execution_profile':'browser-v1', 'image':{'requested':'sha256:'+'a'*64},
                    'implementation':{admission.SCRIPTS[1]:'c'*64},
                    'models':[['gpt-6-sol-high','gpt-6-sol','high']]}
        records=self.model_receipts(expected)
        with self.assertRaisesRegex(admission.AdmissionError, 'execution treatment'):
            admission.validate_model_controls(self.root,expected,records)
        path=self.root/records['gpt-6-sol-high']['receipt']
        receipt=json.loads(path.read_text())
        receipt['browser_image_received']=True
        write_record(path,receipt)
        admission.validate_model_controls(self.root,expected,records)

    def test_browser_task_changes_admission_profile_on_the_same_image(self):
        from types import SimpleNamespace
        sandbox=self.compiled.suite.sandbox
        def fingerprint(compiled):
            image={'Id':sandbox.runtime_image,'Os':'linux','Architecture':'arm64'}
            with patch.object(admission,'preflight_harbor_binary',return_value=SimpleNamespace(version='test',git_commit='test')), \
                 patch.object(suite_run,'_verify_sandbox_runtime'), \
                 patch.object(admission.subprocess,'check_output',side_effect=[json.dumps([image]),'{}']):
                return admission.fingerprint(compiled,'/test/harbor')
        repair=fingerprint(self.compiled)
        self.assertNotIn('execution_profile',repair)
        tasks=self.root/'.openbench/tasks'
        shutil.rmtree(tasks)
        task=admission.ROOT/'benchmarks/harbor/local/activity-explorer-c1-o1'
        shutil.copytree(task,tasks/task.name)
        browser=fingerprint(suite_run.compile_suite(self.compiled.suite.path))
        self.assertEqual(browser['execution_profile'],'browser-v1')
        self.assertEqual(browser['image'],repair['image'])

    def test_qualification_dispatches_a_probe_for_every_selected_pair_before_auth(self):
        from types import SimpleNamespace
        expected = {'image':{'requested':self.compiled.suite.sandbox.runtime_image},
                    'implementation':{admission.SCRIPTS[1]:'c'*64},
                    'models':[['gpt-6-sol-low','gpt-6-sol','low'],
                              ['gpt-6-luna-max','gpt-6-luna','max']]}
        records = admission.model_control_records(expected)
        commands = []
        def offline(command, **kwargs):
            # The actual CLI/control runner is expensive and separately covered
            # by Docker CI. Record its emitted argv and supply synthetic receipts.
            if len(command)>1 and command[1] == admission.SCRIPTS[1]:
                commands.append(command)
                alias = command[command.index('--model')+1]
                target = Path(command[command.index('--output-dir')+1])/'receipt.json'
                target.parent.mkdir(parents=True, exist_ok=True)
                write_record(target, self.model_receipt(expected, alias, records[alias]))
        with patch.object(admission,'fingerprint',return_value=expected), \
             patch.object(admission,'preflight_harbor_binary',return_value=SimpleNamespace()), \
             patch.object(suite_run,'_harbor_python_interpreter',return_value=Path('/fake/python')), \
             patch.object(admission.subprocess,'run',side_effect=offline), \
             patch.object(admission,'prepare_control',side_effect=RuntimeError('stop before auth')):
            with self.assertRaisesRegex(RuntimeError,'stop before auth'):
                admission.qualify(self.compiled,self.root,'/fake/harbor','/never-read-auth')
        self.assertEqual([c[c.index('--model')+1] for c in commands],
                         ['gpt-6-sol-low','gpt-6-luna-max'])
        self.assertEqual([Path(c[c.index('--output-dir')+1]).name for c in commands],
                         ['tool-loop','tool-loop-1'])

    def test_matching_evidence_is_accepted_and_changed_runtime_refused(self):
        path,expected,value=self.receipt()
        self.assertTrue(admission.validate_admission(path,expected)['passed'])
        changed=copy.deepcopy(expected)
        changed['image']['requested']='sha256:'+'d'*64
        with self.assertRaisesRegex(admission.AdmissionError,'stale'):
            admission.validate_admission(path,changed)

    def test_bare_authenticated_control_cannot_admit_captured_context(self):
        path, expected, value = self.receipt()
        expected['context_sha256'] = 'd'*64
        value['fingerprint'] = expected
        write_record(path, value)
        with self.assertRaisesRegex(admission.AdmissionError, 'authenticated control used another'):
            admission.validate_admission(path, expected)

    def test_captured_archive_is_forwarded_to_control_suite(self):
        from obench.frozen_context import freeze_context
        root = self.root.resolve()
        capture = root / 'capture'; (capture/'codex').mkdir(parents=True)
        (capture/'codex/AGENTS.md').write_text('Check the change.\n')
        archive = root / 'context.tar'
        sha = freeze_context(capture, archive)
        suite = self.compiled.suite.path
        suite.write_text(suite.read_text() + 'context_archive=' + json.dumps(str(archive))
                         + '\ncontext_sha256=' + json.dumps(sha) + '\n')
        compiled = suite_run.compile_suite(suite)
        control, _ = admission.prepare_control(compiled, self.root/'captured-control')
        self.assertEqual(control.suite.sandbox.context_sha256, sha)
        self.assertEqual(control.suite.sandbox.context_archive, archive)

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
        for alias, model, effort in [('gpt-5.6-luna-max', 'gpt-5.6-luna', 'max'),
                                     ('gpt-6-sol-low', 'gpt-6-sol', 'low'),
                                     ('gpt-6-luna-high', 'gpt-6-luna', 'high')]:
            changed=copy.deepcopy(expected)
            changed['models']=[[alias, model, effort]]
            value['fingerprint']=changed
            write_record(path,value)
            with self.subTest(model=model), self.assertRaisesRegex(admission.AdmissionError,'another execution treatment'):
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
        expected['.git']=b'gitdir: /tmp/openbench-workspace.git\n'
        from obench.harbor_sandbox import write_files
        write_files(trial/'artifacts/workspace', {name:data for name,data in expected.items() if name.startswith('scripts/profiles/')})
        receipt=trial/'verifier/sandbox-grading.json'
        receipt.write_text(json.dumps({'freeze':{'workspace_files':source_receipt(expected)['files']}}))
        admission.verify_control(self.control,self.task,result_path)
        for change in ('requirements','extra-file','missing-file','missing-marker','missing-git','changed-git'):
            files=dict(expected)
            if change=='requirements': files['requirements.txt']=b'changed dependency\n'
            elif change=='extra-file': files['new.txt']=b'unrequested\n'
            elif change=='missing-file': del files['requirements.txt']
            elif change=='missing-git': del files['.git']
            elif change=='changed-git': files['.git']=b'gitdir: /somewhere/else\n'
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
