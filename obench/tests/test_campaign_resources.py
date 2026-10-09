"""Capacity selection conservatively falls back; prepared suites keep their inputs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from obench import init, suite_run
from obench import campaign_resources as resources

class CapacityTests(unittest.TestCase):
    def healthy(self):
        return {'host_cpus':8,'load_one':2.,'host_available_bytes':12*resources.GIB,
                'docker_cpus':8,'docker_total_bytes':24*resources.GIB,
                'docker_used_bytes':2*resources.GIB,'docker_cpu_cores':1.,'disk_free_bytes':40*resources.GIB}

    def test_capacity_thresholds_and_unknowns_choose_serial(self):
        good=self.healthy()
        self.assertEqual(resources.decide(good)['concurrency'],2)
        for change in ({'host_cpus':2},{'load_one':7.},{'host_available_bytes':2*resources.GIB},
                       {'docker_cpus':2},{'docker_used_bytes':18*resources.GIB},
                       {'docker_cpu_cores':7.},{'disk_free_bytes':5*resources.GIB},
                       {'docker_used_bytes':None},{'load_one':float('nan')}):
            with self.subTest(change=change):
                result=resources.decide({**good,**change})
                self.assertEqual(result['concurrency'],1)
                self.assertTrue(result['reasons'])

    def test_prepare_preserves_source_and_seals_only_concurrency_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            init.init_scaffold(tmp)
            source=Path(tmp)/'.openbench/suites/default.toml'
            original=source.read_bytes()
            target=source.with_name('selected.toml')
            with patch.object(resources,'sample',return_value=self.healthy()):
                result=resources.prepare(source,target)
            self.assertEqual(source.read_bytes(),original)
            selected=suite_run.compile_suite(target)
            before=suite_run.compile_suite(source)
            self.assertEqual(selected.suite.run.concurrency,2)
            self.assertEqual(selected.suite.run.timeout_seconds,before.suite.run.timeout_seconds)
            self.assertEqual(selected.suite.task_sets,before.suite.task_sets)
            self.assertEqual(selected.suite.arms,before.suite.arms)
            evidence=json.loads(Path(result['capacity_receipt']).read_text())
            self.assertEqual(evidence['manifest_sha256'],selected.manifest_sha256)
            with self.assertRaises(FileExistsError): resources.prepare(source,target)
            with self.assertRaisesRegex(ValueError,'same directory'):
                resources.prepare(source,Path(tmp)/'other.toml')

    def test_docker_units_are_bytes_and_invalid_values_fail(self):
        self.assertEqual(resources.memory_bytes('1.5GiB'),int(1.5*resources.GIB))
        self.assertEqual(resources.memory_bytes('512MiB'),resources.GIB//2)
        with self.assertRaises(ValueError): resources.memory_bytes('unknown')

    def test_dispatch_rechecks_and_refuses_changed_capacity(self):
        from types import SimpleNamespace
        compiled=SimpleNamespace(suite=SimpleNamespace(run=SimpleNamespace(concurrency=2),project_root='.'))
        with patch.object(resources,'sample',return_value={**self.healthy(),'host_available_bytes':0}):
            with self.assertRaisesRegex(ValueError,'parallel capacity unavailable'):
                resources.require_capacity(compiled)

    def test_overlap_detector_rejects_serial_and_unbound_evidence(self):
        from obench.runtime_admission import verify_parallel_execution, AdmissionError
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,start,end in [('a',1,4),('b',2,5)]:
                p=root/'.openbench/jobs/job'/name/'result.json';p.parent.mkdir(parents=True)
                p.write_text(json.dumps({'agent_execution':{'started_at':f'2026-10-09T00:00:0{start}Z','finished_at':f'2026-10-09T00:00:0{end}Z'}}))
            self.assertEqual(verify_parallel_execution(root)['peak_agent_executions'],2)
            with self.assertRaisesRegex(AdmissionError,'not bound'):
                verify_parallel_execution(root,evidence_root=root,evidence={})
            p.write_text(json.dumps({'agent_execution':{'started_at':'2026-10-09T00:00:04Z','finished_at':'2026-10-09T00:00:05Z'}}))
            with self.assertRaisesRegex(AdmissionError,'overlapping'):
                verify_parallel_execution(root)
