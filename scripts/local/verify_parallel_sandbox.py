#!/usr/bin/env python3
"""Two overlapping real Harbor sandboxes: private writes and independent cleanup."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts/local')]
from verify_repair_lifecycle import FixtureAgent as BaseFixture
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from obench.harbor_results import _validate_artifacts, _validate_sandbox_receipt

# Both Harbor imports use the same host-side rendezvous. Candidate processes
# never receive this object, the peer environment, or Docker authority.
sys.modules['verify_parallel_sandbox']=sys.modules[__name__]
READY={}
VERIFIED=set()
FINISHED=None

class FixtureAgent(BaseFixture):
    async def run(self,instruction,environment,context):
        result=await environment.exec('printf %s '+shlex.quote(self.mode)+' > /app/parallel-control.txt')
        if result.return_code: raise RuntimeError('private write failed')
        READY[self.mode]=environment
        async def both():
            while len(READY)!=2: await asyncio.sleep(.05)
        await asyncio.wait_for(both(),60)
        peers=list(READY.values())
        if set(peers[0]._containers.values()) & set(peers[1]._containers.values()):
            raise RuntimeError('trial container identities overlap')
        # Read the same pathname in both simultaneously active sandboxes.
        for name,peer in READY.items():
            value=await peer.exec('cat /app/parallel-control.txt')
            if value.return_code or value.stdout!=name: raise RuntimeError('cross-trial source contamination')
        VERIFIED.add(self.mode)
        async def checked():
            while len(VERIFIED)!=2: await asyncio.sleep(.05)
        await asyncio.wait_for(checked(),60)
        if self.mode=='second':
            # The first Trial fully grades/stops/deletes its resources. The
            # second must still support workspace access and gateway traffic.
            await asyncio.wait_for(FINISHED.wait(),150)
            code="import http.client; c=http.client.HTTPConnection('127.0.0.1',8765); c.request('GET','/offline-forbidden-route'); r=c.getresponse(); assert 400<=r.status<500; r.read(); c.close()"
            result=await environment.exec('python3 -c '+shlex.quote(code))
            if result.return_code: raise RuntimeError('peer cleanup disrupted the surviving gateway')
            result=await environment.exec('cat /app/parallel-control.txt')
            if result.return_code or result.stdout!='second': raise RuntimeError('peer cleanup disrupted source')
        context.metadata={'offline_fixture':self.mode,'model_calls':0}
        context.n_input_tokens=context.n_output_tokens=0

async def main():
    global FINISHED
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image',required=True)
    parser.add_argument('--task',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False)
    import tomllib
    metadata=tomllib.loads((args.task/'task.toml').read_text())['metadata']
    oracle=metadata.get('openbench_oracle')
    FINISHED=asyncio.Event()
    async def run(mode):
        config=TrialConfig.model_validate({'task':{'path':str(args.task.resolve())},'trial_name':mode,'trials_dir':str(args.output),
            'agent':{'import_path':'verify_parallel_sandbox:FixtureAgent','kwargs':{'mode':mode}},
            'environment':{'import_path':'obench.harbor_sandbox:RepairSandbox','kwargs':{'runtime_image':args.runtime_image,**({'oracle_id':oracle} if oracle else {})}},
            'verifier':{'import_path':'obench.repair_grading:RepairVerifier' if oracle else 'obench.sandbox_grading:RepairVerifier',
                        'kwargs':{'worker_image':args.runtime_image,**({'oracle_id':oracle} if oracle else {})}}})
        trial=await Trial.create(config)
        result=(await trial.run()).model_dump(mode='json')
        if mode=='first': FINISHED.set()
        if result.get('exception_info'): raise RuntimeError(mode+': lifecycle failed')
        score=result['verifier_result']['rewards']['reward']
        if score!=0: raise RuntimeError('baseline unexpectedly solved')
        trial_dir=args.output/mode
        evidence=json.loads((trial_dir/'verifier/openbench-verifier-evidence.json').read_text())
        _validate_sandbox_receipt(trial_dir,evidence['openbench_task_content_digest'],score,mode,
            expected_image=args.runtime_image,expected_model='gpt-5.6-terra',expected_effort='xhigh',expected_oracle=oracle)
        _validate_artifacts(trial_dir,result,mode,required=True)
        return {'mode':mode,'score':score,'trusted_receipt_valid':True}
    records=await asyncio.gather(run('first'),run('second'))
    summary={'passed':True,'runtime_image':args.runtime_image,'probe_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'controls':records,'overlap_barrier_passed':True,'peer_cleanup_survived':True,'live_inference':False}
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary))

if __name__=='__main__':asyncio.run(main())
