#!/usr/bin/env python3
"""Opt-in, offline Harbor Trial lifecycle controls for the isolated Dojo task.

Requires pinned optional Harbor and a prebuilt immutable repair runtime. Starts
production broker/relay with synthetic noncredentials, sends one rejected route,
and never issues an inference request. This is not a model or whole-job import.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harbor.agents.base import BaseAgent
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from obench.harbor_results import _validate_artifacts, _validate_sandbox_receipt


class FixtureAgent(BaseAgent):
    def __init__(self, *args, mode='baseline', reference=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.reference = Path(reference) if reference else None

    @staticmethod
    def name():
        return 'offline-lifecycle-fixture'

    def version(self):
        return '1'

    async def setup(self, environment):
        with tempfile.TemporaryDirectory(prefix='obench-dummy-auth-') as directory:
            auth = Path(directory) / 'auth.json'
            auth.write_text(json.dumps({'tokens': {
                'access_token': 'offline-not-a-credential', 'account_id': 'offline'}}))
            auth.chmod(0o600)
            await environment.start_gateway('gpt-5.6-terra', 'xhigh', auth)
        result = await environment.exec('python3 -c ' + shlex.quote(
            "import http.client; c=http.client.HTTPConnection('127.0.0.1',8765); "
            "c.request('GET','/offline-forbidden-route'); r=c.getresponse(); "
            "assert 400<=r.status<500,r.status; r.read(); c.close()"))
        if result.return_code != 0:
            raise RuntimeError('offline gateway route rejection failed')

    async def run(self, instruction, environment, context):
        if self.mode == 'reference':
            files = sorted((self.reference / 'scripts/profiles').glob('*.py'))
            if not files:
                raise RuntimeError('reference source is absent')
            for path in files:
                destination = '/tmp/fixture-' + path.name
                await environment.upload_file(path, destination)
                result = await environment.exec('cp ' + shlex.quote(destination) +
                                                ' /app/scripts/profiles/' + shlex.quote(path.name))
                if result.return_code != 0:
                    raise RuntimeError('reference overlay failed')
        elif self.mode == 'malformed':
            result = await environment.exec(
                'rm /app/scripts/profiles/__init__.py && ln -s /etc/passwd /app/scripts/profiles/__init__.py')
            if result.return_code != 0:
                raise RuntimeError('malformed-source control did not activate')
        if self.mode == 'baseline':
            result = await environment.exec("printf 'unsubmitted control' > /app/control-note.txt")
            if result.return_code != 0:
                raise RuntimeError('unsubmitted-file control did not activate')
        context.metadata = {'offline_fixture': self.mode, 'model_calls': 0}
        context.n_input_tokens = 0
        context.n_output_tokens = 0


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', required=True)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True,
                        help='Reference source root containing scripts/profiles')
    parser.add_argument('--output', type=Path, required=True,
                        help='New evidence directory; existing directory is rejected')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    receipts = []
    for mode, expected in [('baseline', 0), ('reference', 1), ('malformed', 0)]:
        config = TrialConfig.model_validate({
            'task': {'path': str(args.task.resolve())},
            'trial_name': mode, 'trials_dir': str(output),
            'agent': {'import_path': 'verify_repair_lifecycle:FixtureAgent',
                      'kwargs': {'mode': mode, 'reference': str(args.reference.resolve())}},
            'environment': {'import_path': 'obench.harbor_sandbox:RepairSandbox',
                            'kwargs': {'runtime_image': args.runtime_image}},
            'verifier': {'import_path': 'obench.sandbox_grading:RepairVerifier',
                         'kwargs': {'worker_image': args.runtime_image}},
        })
        trial = await Trial.create(config)
        result = (await trial.run()).model_dump(mode='json')
        trial_dir = output / mode
        if result.get('exception_info') is not None:
            raise RuntimeError(f'{mode}: Harbor trial failed; inspect {trial_dir}')
        score = result['verifier_result']['rewards']['reward']
        if score != expected:
            raise RuntimeError(f'{mode}: expected reward {expected}, observed {score}')
        evidence = json.loads((trial_dir / 'verifier/openbench-verifier-evidence.json').read_text())
        receipt_path = _validate_sandbox_receipt(
            trial_dir, evidence['openbench_task_content_digest'], score, mode,
            expected_image=args.runtime_image, expected_model='gpt-5.6-terra', expected_effort='xhigh')
        receipt = json.loads(receipt_path.read_text())
        if mode == 'baseline':
            metadata = receipt['freeze']['workspace_files'].get('control-note.txt')
            if metadata != {'bytes': len(b'unsubmitted control'), 'sha256': hashlib.sha256(b'unsubmitted control').hexdigest()}:
                raise RuntimeError('full workspace metadata did not capture unsubmitted file')
            if 'control-note.txt' in receipt['freeze']['files']:
                raise RuntimeError('unsubmitted file escaped source allowlist')
        failure = receipt['grading'].get('candidate_failure')
        rejected = failure == 'invalid_source_artifact'
        if rejected != (mode == 'malformed'):
            raise RuntimeError(f'{mode}: invalid-source control classification mismatch')
        _, workspace_digest = _validate_artifacts(
            trial_dir, result, mode, required=True, rejected_source=rejected)
        ledger = [json.loads(line) for line in
                  (trial_dir / 'verifier/sandbox-gateway.jsonl').read_text().splitlines()]
        if any(row.get('event') == 'request' for row in ledger):
            raise RuntimeError('offline control unexpectedly forwarded an inference request')
        record = {'mode': mode, 'reward': score, 'exception_info': None,
                  'candidate_failure': failure, 'workspace_sha256': workspace_digest,
                  'trusted_receipt_valid': True, 'artifact_import_valid': True,
                  'gateway_events': ledger}
        receipts.append(record)
        (output / 'summary.json').write_text(json.dumps({
            'scope': 'Actual Harbor Trial lifecycle; synthetic agent; no model inference; not a whole-job import',
            'runtime_image': args.runtime_image,
            'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'controls': receipts}, indent=2) + '\n')
        print(json.dumps(record), flush=True)
    print('ALL THREE HARBOR LIFECYCLE CONTROLS PASSED', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
