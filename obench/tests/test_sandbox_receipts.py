"""Tampering must prevent a sandbox result from entering comparisons."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from obench.harbor_results import _validate_sandbox_receipt
from obench.harbor_agents.sandbox_codex import staged_auth_paths


class SandboxReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.verifier = self.root / 'verifier'
        self.verifier.mkdir()
        self.binding = {'scheme': 3, 'schema': 'openbench-isolated-repair-task-v1'}
        self.digest = {'scheme': 3, 'sha256': hashlib.sha256(json.dumps(
            self.binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
        ledger = b'{"event":"stopped","role":"broker","clean":true}\n'
        (self.verifier / 'sandbox-gateway.jsonl').write_bytes(ledger)
        self.receipt = {'task_binding': self.binding,
            'freeze': {'solver_stopped': True, 'broker_revoked': True,
                       'gateway_ledger_sha256': hashlib.sha256(ledger).hexdigest()},
            'grading': {'score': 1, 'worker': {'network': 'none', 'user': '10001:10001',
                'host_mounts': False, 'read_only_root': True, 'capabilities': 'none'}}}

    def validate(self, score=1):
        (self.verifier / 'sandbox-grading.json').write_text(json.dumps(self.receipt))
        return _validate_sandbox_receipt(self.root, self.digest, score, 'test')

    def test_accepts_bound_receipt_and_candidate_failure_zero(self):
        self.assertEqual(self.validate().name, 'sandbox-grading.json')
        self.receipt['grading'] = {'score': 0, 'worker': None, 'candidate_failure': 'invalid_source_artifact'}
        self.validate(0)
        with self.assertRaises(ValueError):
            self.validate(1)

    def test_rejects_oracle_score_and_shutdown_tampering(self):
        for section, key, value in [('freeze','solver_stopped',False),
                                    ('freeze','broker_revoked',False),
                                    ('grading','score',0),
                                    ('task_binding','schema','changed')]:
            with self.subTest(key=key):
                old = self.receipt[section][key]
                self.receipt[section][key] = value
                with self.assertRaises(ValueError): self.validate()
                self.receipt[section][key] = old
        self.receipt['grading']['worker']['host_mounts'] = True
        with self.assertRaises(ValueError): self.validate()

    def test_rejects_gateway_ledger_tampering_or_removal(self):
        ledger = self.verifier / 'sandbox-gateway.jsonl'
        ledger.write_text('{"event":"forged"}\n')
        with self.assertRaises(ValueError): self.validate()
        ledger.unlink()
        with self.assertRaises(ValueError): self.validate()


    def test_registered_receipt_binds_oracle_protocol_and_digest_scheme(self):
        oracle='agentmonitor-benchmark-v2'
        protocol='am-benchmark-observations-v1'
        self.binding.update(scheme=4,schema='openbench-isolated-repair-task-v2',oracle={'id':oracle,'protocol':protocol})
        self.digest={'scheme':4,'sha256':hashlib.sha256(json.dumps(self.binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        self.receipt['grading'].update(oracle_id=oracle,protocol=protocol)
        def check(expected=oracle):
            (self.verifier/'sandbox-grading.json').write_text(json.dumps(self.receipt))
            return _validate_sandbox_receipt(self.root,self.digest,1,'test',expected_oracle=expected)
        check()
        for expected in (None,'unknown'):
            with self.subTest(expected=expected),self.assertRaises(ValueError):check(expected)
        self.receipt['grading']['protocol']='another-protocol'
        with self.assertRaisesRegex(ValueError,'oracle differs'):check()


class StagedAuthTests(unittest.TestCase):
    def test_private_staging_allows_repeat_return_but_rejects_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, returned = root / 'auth.json', root / 'auth-return.json'
            source.write_text('{}'); source.chmod(0o600)
            self.assertEqual(staged_auth_paths(str(source), str(returned)), (source, returned))
            returned.write_text('{}')
            staged_auth_paths(str(source), str(returned))
            returned.unlink(); returned.symlink_to(source)
            with self.assertRaises(RuntimeError): staged_auth_paths(str(source), str(returned))
            with self.assertRaises(RuntimeError): staged_auth_paths(str(source), str(source))
            returned.unlink(); source.chmod(0o644)
            with self.assertRaises(RuntimeError): staged_auth_paths(str(source), str(returned))
