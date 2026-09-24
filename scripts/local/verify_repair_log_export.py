#!/usr/bin/env python3
"""Offline real-Harbor controls for large logs, timeout capture and refusal.

Uses synthetic noncredentials, never model inference. Requires pinned Harbor
and an immutable repair runtime. Evidence is local-only.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from verify_repair_lifecycle import FixtureAgent
from obench.harbor_sandbox import MAX_LOG_FILE_BYTES

LOG_BYTES = 3 * 1024 * 1024


class LogFixtureAgent(FixtureAgent):
    async def run(self, instruction, environment, context):
        size = MAX_LOG_FILE_BYTES + 1 if self.mode == 'oversize' else LOG_BYTES
        program = ("from pathlib import Path; "
                   f"Path('/logs/agent/payload.log').write_bytes(b'x' * {size})")
        if self.mode == 'symlink':
            program += "; Path('/logs/agent/unsafe').symlink_to('/etc/passwd')"
        result = await environment.exec('python3 -c ' + shlex.quote(program))
        if result.return_code:
            raise RuntimeError('log fixture creation failed')
        context.metadata = {'offline_fixture': self.mode, 'model_calls': 0}
        context.n_input_tokens = context.n_output_tokens = 0
        if self.mode == 'timeout':
            await asyncio.sleep(60)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', required=True)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for mode in ('complete', 'timeout', 'oversize', 'symlink'):
        config = TrialConfig.model_validate({
            'task': {'path': str(args.task.resolve())},
            'trial_name': mode, 'trials_dir': str(output),
            'agent': {'import_path': 'verify_repair_log_export:LogFixtureAgent',
                      'override_timeout_sec': 3 if mode == 'timeout' else 30,
                      'kwargs': {'mode': mode}},
            'environment': {'import_path': 'obench.harbor_sandbox:RepairSandbox',
                            'kwargs': {'runtime_image': args.runtime_image}},
            'verifier': {'import_path': 'obench.sandbox_grading:RepairVerifier',
                         'kwargs': {'worker_image': args.runtime_image}},
        })
        trial = await Trial.create(config)
        result = (await trial.run()).model_dump(mode='json')
        trial_dir = output / mode
        log = trial_dir / 'agent/payload.log'
        diagnostics = list((trial_dir / 'verifier/sandbox-exports').glob('*.json'))
        exception = (result.get('exception_info') or {}).get('exception_type')
        if mode in ('complete', 'timeout'):
            assert log.stat().st_size == LOG_BYTES
            assert hashlib.sha256(log.read_bytes()).hexdigest() == hashlib.sha256(b'x' * LOG_BYTES).hexdigest()
            assert not diagnostics
            assert exception == ('AgentTimeoutError' if mode == 'timeout' else None), exception
        else:
            assert not log.exists(), 'refused archive partially exported'
            assert len(diagnostics) == 1
            report = json.loads(diagnostics[0].read_text())
            assert report['source'] == '/logs/agent'
            if mode == 'oversize':
                assert report['violation']['observed'] == MAX_LOG_FILE_BYTES + 1
                assert report['violation']['limit'] == MAX_LOG_FILE_BYTES
            else:
                assert report['error'] == 'archive contains links or special files'
            # Harbor must retain the cleanup/export error even though it catches
            # the initial download exception.
            assert 'log export failed' in (trial_dir / 'trial.log').read_text()
        receipt = json.loads((trial_dir / 'verifier/sandbox-grading.json').read_text())
        assert receipt['freeze']['solver_stopped'] and receipt['freeze']['broker_revoked']
        records.append({'mode': mode, 'exception': exception, 'log_bytes': log.stat().st_size if log.exists() else None,
                        'diagnostics': len(diagnostics), 'clean_shutdown': True})
        print(json.dumps(records[-1]), flush=True)
    (output / 'summary.json').write_text(json.dumps({
        'scope': 'real Harbor lifecycle; synthetic logs, no model calls or calibration',
        'runtime_image': args.runtime_image,
        'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'controls': records}, indent=2) + '\n')


if __name__ == '__main__':
    asyncio.run(main())
