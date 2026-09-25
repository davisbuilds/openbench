"""Oracle selection and versioned identity controls; no Harbor dependency."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from obench.repair_oracles import registry
from obench.repair_worker import strict_json
from obench.sandbox_grading import GradingError, task_digest as legacy_digest


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'task'
        old=Path(__file__).resolve().parents[2]/'harbor-tasks-local/dojo-evidence-pr60-v4'
        shutil.copytree(old,self.root)
        config=self.root/'task.toml'
        text=config.read_text().replace('dojo-evidence-pr60-v4','am-benchmark-pr106-v3').replace('[metadata]\n','[metadata]\nopenbench_oracle = "agentmonitor-benchmark-v2"\n').replace('scheme = 3','scheme = 4')
        config.write_text(text)
        self.reseal()

    def reseal(self):
        import re
        value=registry.task_digest(self.root)
        config=self.root/'task.toml'
        config.write_text(re.sub(r'sha256 = "[a-f0-9]{64}"','sha256 = "'+value+'"',config.read_text()))
        return {'scheme':4,'sha256':value}

    def test_known_binding_accepts_and_changed_task_refuses(self):
        binding=self.reseal()
        manifest=registry.validate_task_binding(self.root,binding)
        self.assertEqual(manifest['oracle']['protocol'],'am-benchmark-observations-v1')
        (self.root/'instruction.md').write_text('changed')
        with self.assertRaisesRegex(GradingError,'binding mismatch'):
            registry.validate_task_binding(self.root,binding)

    def test_unknown_or_task_mismatched_registry_selection_fails(self):
        for metadata in ({'openbench_oracle':'os:system'},
                         {'openbench_oracle':'agentmonitor-benchmark-v2','openbench_task':'other'}):
            with self.subTest(metadata=metadata),self.assertRaises(GradingError):
                registry.select(metadata)

    def test_legacy_dojo_digest_is_unchanged(self):
        import tomllib
        task=Path(__file__).resolve().parents[2]/'harbor-tasks-local/dojo-evidence-pr60-v4'
        expected=tomllib.loads((task/'task.toml').read_text())['metadata']['openbench_task_content_digest']
        self.assertEqual(expected,{'scheme':3,'sha256':legacy_digest(task)})

    def test_worker_protocol_rejects_duplicates_and_nonfinite(self):
        self.assertEqual(strict_json('{"value":0.5}'),{'value':.5})
        for data in ('{"value":1,"value":2}','{"value":1e999}','{"value":NaN}'):
            with self.subTest(data=data),self.assertRaises(ValueError): strict_json(data)


class AgentMonitorOracleTests(unittest.TestCase):
    def test_all_failed_observations_are_zero_and_guards_gate_score(self):
        from obench.repair_oracles import agentmonitor as oracle
        fixtures=oracle.cases()
        self.assertEqual([name for name,_,_ in fixtures],['ID1','ID2','ID3','ID4','MIG1','MIG2','GRID1','GRID2','GUARD1','GUARD2'])
        self.assertEqual(oracle.grade([{'ok':False} for _ in fixtures])['score'],0)
        for name,_,_ in fixtures:
            self.assertFalse(oracle.compare(name,[]))
        self.assertNotIn('expected',json.dumps([request for _,_,request in fixtures]))
        self.assertNotIn('assert',oracle.NODE_PROTOCOL)

    def test_identity_accepts_arbitrary_nonempty_study_representation(self):
        from obench.repair_oracles import agentmonitor as oracle
        good=[None,{'eventsImported':1},{'duplicates':1},{'duplicates':1},[{'study_id':'/canonical/path'}]]
        self.assertTrue(oracle.compare('ID2',good))
        good[-1][0]['study_id']=''
        self.assertFalse(oracle.compare('ID2',good))

    def test_coverage_checks_observations_without_prescribing_expected_trials(self):
        from obench.repair_oracles import agentmonitor as oracle
        good=[{'arms':[{'canonical_model':'partial','n':2,'mean_score':.7,'excluded_trials':2,'expected_trials':42},
                       {'canonical_model':'complete','n':4,'mean_score':1,'excluded_trials':0}]}]
        self.assertTrue(oracle.compare('GRID1',good))
        for key,bad in (('n',3),('mean_score',.5),('excluded_trials',1)):
            import copy
            changed=copy.deepcopy(good);changed[0]['arms'][0][key]=bad
            self.assertFalse(oracle.compare('GRID1',changed))

    def test_source_policy_allows_new_helpers_without_allowing_metadata(self):
        oracle=registry.ORACLES['agentmonitor-benchmark-v2']
        self.assertEqual(registry.source_names(oracle,['src/new-helper.ts','src/db/schema.ts','package.json']),
                         {'src/new-helper.ts','src/db/schema.ts'})


class RegisteredSuiteTests(unittest.TestCase):
    def test_registered_task_compiles_into_locked_oracle_and_isolated_plugins(self):
        from obench import init,suite_run
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            init.init_scaffold(root)
            tasks=root/'.openbench/tasks'
            shutil.rmtree(tasks)
            source=Path(__file__).resolve().parents[2]/'harbor-tasks-local/am-benchmark-pr106-v3'
            shutil.copytree(source,tasks/source.name)
            suite=root/'.openbench/suites/default.toml'
            suite.write_text(suite.read_text().replace('gpt-5.6-sol','gpt-5.6-terra-xhigh')+'\n[sandbox]\nkind="repair-v1"\nruntime_image="sha256:'+'a'*64+'"\n')
            compiled=suite_run.compile_suite(suite)
            job=suite_run.plan_jobs(compiled)[0].artifact.as_dict()
            self.assertEqual(job['environment']['kwargs']['oracle_id'],'agentmonitor-benchmark-v2')
            self.assertEqual(job['verifier']['import_path'],'obench.repair_grading:RepairVerifier')
            self.assertIn('obench.repair_oracles.agentmonitor',compiled.manifest['sandbox']['implementation_sha256'])
            from obench import runtime_admission
            control,_=runtime_admission.prepare_control(compiled,root/'control')
            self.assertEqual(control.task_sets[0].task_names,('dojo-evidence-pr60-v4',))
