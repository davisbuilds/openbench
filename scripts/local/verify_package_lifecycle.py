#!/usr/bin/env python3
"""Offline Harbor lifecycle and pinned private-package result controls."""
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
sys.path.insert(0,str(ROOT/'scripts/ci'))
from browser_quality_fixtures import prepare
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from obench.harbor_results import _validate_artifacts,_validate_sandbox_receipt

ORACLE='activity-explorer-v2'


class FixtureAgent(DojoFixture):
    async def setup(self,environment):
        await super().setup(environment)
        with tempfile.NamedTemporaryFile(prefix='obench-oracle-host-canary-') as canary:
            canary.write(b'host-only');canary.flush()
            code=("from pathlib import Path; "
                  f"assert Path('/app/{self.artifact}').is_file(); "
                  f"assert not Path({canary.name!r}).exists(); "
                  "assert not Path('/opt/openbench/obench/repair_oracles').exists(); "
                  "assert not Path('/opt/openbench/obench/repair_grading.py').exists()")
            result=await environment.exec('python3 -c '+shlex.quote(code))
            if result.return_code: raise RuntimeError('paired workspace/host/oracle read control failed')

    async def run(self,instruction,environment,context):
        if self.mode=='reference':
            for source in sorted(self.reference.rglob('*')):
                if not source.is_file(): continue
                relative = source.relative_to(self.reference).as_posix()
                destination='/tmp/fixture-'+source.name
                await environment.upload_file(source,destination)
                result=await environment.exec('mkdir -p '+shlex.quote(str(Path('/app')/Path(relative).parent))+' && cp '+shlex.quote(destination)+' '+shlex.quote('/app/'+relative))
                if result.return_code: raise RuntimeError('reference overlay failed')
        if self.mode=='malformed':
            target = shlex.quote('/app/' + self.artifact)
            result=await environment.exec('rm '+target+' && ln -s /etc/passwd '+target)
            if result.return_code: raise RuntimeError('malformed source control failed')
        if self.mode=='forged-reward':
            result=await environment.exec("printf '1.0\\n' > /logs/verifier/reward.txt")
            if result.return_code: raise RuntimeError('candidate reward write control did not activate')
        context.metadata={'offline_fixture':self.mode,'model_calls':0}
        context.n_input_tokens=context.n_output_tokens=0

    def __init__(self, *args, artifact='web/app.js', **kwargs):
        super().__init__(*args, **kwargs)
        if artifact not in ('web/app.js', 'submission/review.json'):
            raise ValueError('unsupported lifecycle artifact')
        self.artifact = artifact


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--task', type=Path, required=True)
    parser.add_argument('--trust-evaluator', action='store_true')
    args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    from obench.package_campaign import pin
    from obench.harbor_sandbox import write_files
    authorization = pin(args.archive, args.sha256, args.trust_evaluator)
    package = authorization.load()
    artifact = 'submission/review.json' if package.descriptor['backend'] == 'artifact-review-v1' else 'web/app.js'
    if package.descriptor['split'] != 'development':
        raise ValueError('lifecycle qualification requires a development package')
    reference = output/'reference-source'
    write_files(reference, {n.removeprefix('controls/reference/'): b
                           for n,b in package.archive.files.items() if n.startswith('controls/reference/')})
    task=args.task.resolve()
    records=[]
    for mode,expected in [('reference',1),('malformed',0),('forged-reward',None)]:
        config=TrialConfig.model_validate({
            'task':{'path':str(task)},'trial_name':mode,'trials_dir':str(output),
            'agent':{'import_path':'verify_package_lifecycle:FixtureAgent',
                     'kwargs':{'mode':mode,'reference':str(reference),'artifact':artifact}},
            'environment':{'import_path':'obench.harbor_sandbox:RepairSandbox',
                           'kwargs':{'runtime_image':args.runtime_image, **authorization.kwargs()}},
            'verifier':{'import_path':'obench.package_campaign:PackageVerifier',
                        'kwargs':{'worker_image':args.runtime_image, **authorization.kwargs()}}})
        trial=await Trial.create(config)
        result=(await trial.run()).model_dump(mode='json')
        trial_dir=output/mode
        if result.get('exception_info') is not None: raise RuntimeError(f'{mode}: lifecycle failed; inspect {trial_dir}')
        score=result['verifier_result']['rewards']['reward']
        if (expected is None and score == 1) or (expected is not None and score != expected): raise RuntimeError(f'{mode}: expected {expected}, observed {score}; inspect {trial_dir}')
        evidence=json.loads((trial_dir/'verifier/openbench-verifier-evidence.json').read_text())
        receipt_path=_validate_sandbox_receipt(trial_dir,evidence['openbench_task_content_digest'],score,mode,
            expected_image=args.runtime_image,expected_model='gpt-5.6-terra',expected_effort='xhigh',expected_package=args.sha256)
        receipt=json.loads(receipt_path.read_text())
        rejected=receipt['grading'].get('candidate_failure')=='invalid_source_artifact'
        if rejected!=(mode=='malformed'): raise RuntimeError('invalid source classification differs')
        _validate_artifacts(trial_dir,result,mode,required=True,rejected_source=rejected)
        ledger=[json.loads(line) for line in (trial_dir/'verifier/sandbox-gateway.jsonl').read_text().splitlines()]
        if any(row.get('event')=='request' for row in ledger): raise RuntimeError('unexpected inference request')
        record={'mode':mode,'reward':score,'source_rejected':rejected,'trusted_receipt_valid':True,'model_calls':0}
        records.append(record);print(json.dumps(record),flush=True)
        (output/'summary.json').write_text(json.dumps({'passed':len(records)==3,'runtime_image':args.runtime_image,'package_sha256':args.sha256,'controls':records},indent=2)+'\n')


if __name__=='__main__': asyncio.run(main())
