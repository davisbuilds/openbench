"""Durable partial evidence and process interruption use real files/processes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from obench import repair_evidence as re


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'attempt'

    def test_completed_private_artifacts_and_changed_evidence(self):
        with re.Operation(self.directory, 'test') as op:
            with op.stage('control:one'):
                artifact = op.artifact('observation', {'observed': 'fresh'})
            op.finish({'status': 'passed'})
        value = re.status(self.directory)
        self.assertEqual(value['status'], 'passed')
        self.assertFalse(value['active'])
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertGreaterEqual(value['stages'][0]['elapsed_seconds'], 0)
        artifact.write_text('{}')
        self.assertEqual(re.status(self.directory)['status'], 'evidence_invalid')
        with self.assertRaises(FileExistsError):
            re.Operation(self.directory, 'again')

    def test_exception_preserves_completed_controls_and_failing_stage(self):
        with self.assertRaisesRegex(ValueError, 'failed worker'):
            with re.Operation(self.directory, 'test') as op:
                with op.stage('first'):
                    op.artifact('observation', {'complete': True})
                with op.stage('second'):
                    raise ValueError('failed worker')
        value = re.status(self.directory)
        self.assertEqual(value['status'], 'incomplete')
        self.assertEqual(len(value['evidence']), 1)
        self.assertEqual(value['stages'][1]['error'], 'failed worker')

    def test_hard_exit_retains_partial_evidence_without_claiming_running(self):
        program = '''
import os, sys
from obench.repair_evidence import Operation
with Operation(sys.argv[1], 'test') as op:
    with op.stage('one'):
        op.artifact('observation', {'complete': True})
    with op.stage('two'):
        os._exit(17)
'''
        process = subprocess.run([sys.executable, '-c', program, str(self.directory)], capture_output=True)
        self.assertEqual(process.returncode, 17, process.stderr)
        value = re.status(self.directory)
        self.assertEqual(value['status'], 'interrupted')
        self.assertEqual(len(value['evidence']), 1)
        self.assertEqual(value['stages'][1]['status'], 'running')
        result = subprocess.run([sys.executable, '-m', 'obench', 'repair', 'status',
                                 str(self.directory), '--json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'interrupted')

    def test_persistence_failure_cannot_finish_as_passed(self):
        with self.assertRaisesRegex(OSError, 'disk full'):
            with re.Operation(self.directory, 'test') as op:
                with patch.object(re, 'write_json', side_effect=OSError('disk full')):
                    op.finish({'status': 'passed'})
        self.assertEqual(re.status(self.directory)['status'], 'incomplete')

    def test_atomic_write_failure_preserves_original_and_refuses_overwrite(self):
        path = Path(self.temp.name) / 'record.json'
        re.write_json(path, {'original': True}, exclusive=True)
        with self.assertRaises(FileExistsError):
            re.write_json(path, {'new': True}, exclusive=True)
        with self.assertRaises(ValueError):
            re.write_json(path, {'bad': float('nan')})
        self.assertEqual(json.loads(path.read_text()), {'original': True})
        self.assertEqual(list(path.parent.glob('.record-*')), [])


class DiagnosticProtocolTests(unittest.TestCase):
    def test_errors_are_bounded_untrusted_observations(self):
        from obench.sandbox_grading import valid_worker_result
        self.assertTrue(valid_worker_result({'ok': False}))
        self.assertTrue(valid_worker_result({'ok': False, 'error': {
            'type': 'ValueError', 'message': 'bad input', 'operation': '2:import'}}))
        for value in (
            {'ok': True, 'error': {}},
            {'ok': False, 'error': {'type': 'Error', 'message': 'x'*1025, 'operation': 'read'}},
            {'ok': False, 'error': {'type': 'Error', 'message': 'bad', 'operation': []}},
        ):
            self.assertFalse(valid_worker_result(value))
