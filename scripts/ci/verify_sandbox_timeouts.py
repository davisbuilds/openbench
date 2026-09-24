#!/usr/bin/env python3
"""Real Harbor/Docker deadline controls with synthetic model work and dummy auth."""
import argparse
import asyncio
import json
from pathlib import Path
import shlex
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harbor.agents.installed.codex import Codex
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from obench.harbor_agents.sandbox_codex import SandboxCodex, CLI_VERSION
from obench.harbor_sandbox import RepairSandbox


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', required=True)
    parser.add_argument('--task', type=Path, default=ROOT / 'harbor-tasks-local/dojo-evidence-pr60-v3')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    original_run, original_seal = SandboxCodex.run, RepairSandbox.seal
    for mode in ('near-deadline', 'timeout'):
        timing = {}
        budget = 8

        async def observed_run(self, instruction, environment, context):
            timing['start'] = time.monotonic()
            try:
                return await original_run(self, instruction, environment, context)
            finally:
                timing['run_return'] = time.monotonic()

        async def synthetic_work(self, instruction, environment, context):
            result = await environment.exec('python3 -c ' + shlex.quote(
                "from pathlib import Path; Path('/logs/agent/fixture.log').write_text('synthetic work')"))
            assert result.return_code == 0
            if mode == 'timeout':
                await asyncio.sleep(budget * 2)
            else:
                remaining = budget - (time.monotonic() - timing['start']) - .5
                assert remaining > 0, 'fixture setup exhausted the execution budget'
                await asyncio.sleep(remaining)
                timing['work_complete'] = time.monotonic()
            context.n_input_tokens = context.n_output_tokens = 0
            context.metadata = {'synthetic_work': True}

        async def slow_seal(self):
            if not self._sealed:
                # Deterministically carry cleanup beyond the agent deadline.
                await asyncio.sleep(1)
            receipt = await original_seal(self)
            assert receipt['solver_stopped'] and receipt['broker_revoked']
            timing.setdefault('sealed', time.monotonic())
            return receipt

        with tempfile.TemporaryDirectory(prefix='obench-synthetic-auth-') as directory:
            staging = Path(directory)
            staging.chmod(0o700)
            auth, returned = staging / 'input.json', staging / 'return.json'
            auth.write_text(json.dumps({'tokens': {'access_token': 'offline-not-a-credential',
                                                    'account_id': 'offline'}}))
            auth.chmod(0o600)
            config = TrialConfig.model_validate({
                'task': {'path': str(args.task.resolve())},
                'trial_name': mode, 'trials_dir': str(args.output.resolve()),
                'agent': {'import_path': 'obench.harbor_agents.sandbox_codex:SandboxCodex',
                          'model_name': 'gpt-5.6-terra', 'override_timeout_sec': budget,
                          'kwargs': {'version': CLI_VERSION, 'reasoning_effort': 'xhigh'},
                          'env': {'CODEX_AUTH_JSON_PATH': str(auth),
                                  'OPENBENCH_CODEX_AUTH_RETURN_PATH': str(returned)}},
                'environment': {'import_path': 'obench.harbor_sandbox:RepairSandbox',
                                'kwargs': {'runtime_image': args.runtime_image}},
                'verifier': {'import_path': 'obench.sandbox_grading:RepairVerifier',
                             'kwargs': {'worker_image': args.runtime_image}},
            })
            with patch.object(Codex, 'run', synthetic_work), \
                 patch.object(SandboxCodex, 'run', observed_run), \
                 patch.object(RepairSandbox, 'seal', slow_seal):
                trial = await Trial.create(config)
                result = (await trial.run()).model_dump(mode='json')
            failure = (result.get('exception_info') or {}).get('exception_type')
            assert failure == ('AgentTimeoutError' if mode == 'timeout' else None), result.get('exception_info')
            assert returned.read_bytes() == auth.read_bytes()
            assert timing['sealed'] > timing['run_return']
            if mode == 'near-deadline':
                assert timing['work_complete'] - timing['start'] < budget
                assert timing['sealed'] - timing['start'] > budget
            trial_dir = args.output / mode
            assert (trial_dir / 'agent/fixture.log').read_text() == 'synthetic work'
            ledger = [json.loads(line) for line in
                      (trial_dir / 'verifier/sandbox-gateway.jsonl').read_text().splitlines()]
            assert not any(row.get('event') == 'request' for row in ledger), 'unexpected model request'
            receipt = json.loads((trial_dir / 'verifier/sandbox-grading.json').read_text())
            assert receipt['freeze']['solver_stopped'] and receipt['freeze']['broker_revoked']
            records.append({'mode': mode, 'exception': failure,
                            'agent_seconds': timing['run_return'] - timing['start'],
                            'sealed_seconds': timing['sealed'] - timing['start'],
                            'model_requests': 0})
            print(json.dumps(records[-1]), flush=True)
    (args.output / 'summary.json').write_text(json.dumps(records, indent=2) + '\n')


if __name__ == '__main__':
    asyncio.run(main())
