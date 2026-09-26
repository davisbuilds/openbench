#!/usr/bin/env python3
"""Offline Harbor lifecycle and oracle-boundary controls for AgentMonitor #106."""
import argparse
import asyncio
import json
from pathlib import Path
import shlex
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from verify_repair_lifecycle import FixtureAgent as DojoFixture
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from obench.harbor_results import _validate_artifacts,_validate_sandbox_receipt

ORACLE='agentmonitor-benchmark-v2'


class FixtureAgent(DojoFixture):
    async def setup(self,environment):
        await super().setup(environment)
        with tempfile.NamedTemporaryFile(prefix='obench-oracle-host-canary-') as canary:
            canary.write(b'host-only');canary.flush()
            code=("from pathlib import Path; "
                  "assert Path('/app/src/db/schema.ts').is_file(); "
                  f"assert not Path({canary.name!r}).exists(); "
                  "assert not Path('/opt/openbench/obench/repair_oracles').exists(); "
                  "assert not Path('/opt/openbench/obench/repair_grading.py').exists()")
            result=await environment.exec('python3 -c '+shlex.quote(code))
            if result.return_code: raise RuntimeError('paired workspace/host/oracle read control failed')

    async def run(self,instruction,environment,context):
        if self.mode in ('reference','helper'):
            for source in sorted(self.reference.rglob('*.ts')):
                relative=source.relative_to(self.reference).as_posix()
                destination='/tmp/fixture-'+source.name
                await environment.upload_file(source,destination)
                result=await environment.exec('cp '+shlex.quote(destination)+' '+shlex.quote('/app/'+relative))
                if result.return_code: raise RuntimeError('reference overlay failed')
        if self.mode=='helper':
            code="from pathlib import Path; p=Path('/app/src/db/schema.ts'); p.write_text(\"import { marker } from '../repair-control.js';\\nif (marker !== 'helper') throw new Error('missing helper');\\n\"+p.read_text()); Path('/app/src/repair-control.ts').write_text(\"export const marker = 'helper';\\n\")"
            result=await environment.exec('python3 -c '+shlex.quote(code))
            if result.return_code: raise RuntimeError('new helper control failed')
        if self.mode=='malformed':
            result=await environment.exec('rm /app/src/db/schema.ts && ln -s /etc/passwd /app/src/db/schema.ts')
            if result.return_code: raise RuntimeError('malformed source control failed')
        if self.mode=='forged-reward':
            result=await environment.exec("printf '1.0\\n' > /logs/verifier/reward.txt")
            if result.return_code: raise RuntimeError('candidate reward write control did not activate')
        context.metadata={'offline_fixture':self.mode,'model_calls':0}
        context.n_input_tokens=context.n_output_tokens=0


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    task=ROOT/'harbor-tasks-local/am-benchmark-pr106-v3'
    records=[]
    for mode,expected in [('baseline',0),('reference',1),('helper',1),('malformed',0),('forged-reward',0)]:
        config=TrialConfig.model_validate({
            'task':{'path':str(task)},'trial_name':mode,'trials_dir':str(output),
            'agent':{'import_path':'verify_registered_lifecycle:FixtureAgent',
                     'kwargs':{'mode':mode,'reference':str(ROOT/'tasks-local/am-benchmark-pr106-v2/solution')}},
            'environment':{'import_path':'obench.harbor_sandbox:RepairSandbox',
                           'kwargs':{'runtime_image':args.runtime_image,'oracle_id':ORACLE}},
            'verifier':{'import_path':'obench.repair_grading:RepairVerifier',
                        'kwargs':{'worker_image':args.runtime_image,'oracle_id':ORACLE}}})
        trial=await Trial.create(config)
        result=(await trial.run()).model_dump(mode='json')
        trial_dir=output/mode
        if result.get('exception_info') is not None: raise RuntimeError(f'{mode}: lifecycle failed; inspect {trial_dir}')
        score=result['verifier_result']['rewards']['reward']
        if score!=expected: raise RuntimeError(f'{mode}: expected {expected}, observed {score}; inspect {trial_dir}')
        evidence=json.loads((trial_dir/'verifier/openbench-verifier-evidence.json').read_text())
        receipt_path=_validate_sandbox_receipt(trial_dir,evidence['openbench_task_content_digest'],score,mode,
            expected_image=args.runtime_image,expected_model='gpt-5.6-terra',expected_effort='xhigh',expected_oracle=ORACLE)
        receipt=json.loads(receipt_path.read_text())
        rejected=receipt['grading'].get('candidate_failure')=='invalid_source_artifact'
        if rejected!=(mode=='malformed'): raise RuntimeError('invalid source classification differs')
        _validate_artifacts(trial_dir,result,mode,required=True,rejected_source=rejected)
        if mode=='helper' and 'src/repair-control.ts' not in receipt['freeze']['files']:
            raise RuntimeError('added helper was not included in frozen source')
        ledger=[json.loads(line) for line in (trial_dir/'verifier/sandbox-gateway.jsonl').read_text().splitlines()]
        if any(row.get('event')=='request' for row in ledger): raise RuntimeError('unexpected inference request')
        record={'mode':mode,'reward':score,'source_rejected':rejected,'trusted_receipt_valid':True,'model_calls':0}
        records.append(record);print(json.dumps(record),flush=True)
        (output/'summary.json').write_text(json.dumps({'runtime_image':args.runtime_image,'controls':records},indent=2)+'\n')


if __name__=='__main__': asyncio.run(main())
