#!/usr/bin/env python3
"""Real Chromium controls for the synthetic activity explorer; no inference."""
import argparse
import base64
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts/ci')]
from browser_quality_fixtures import prepare
from obench.repair_grading import grade_submission
from obench.repair_oracles.registry import ORACLES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workflow',type=Path,help='Also validate and freshly replay task admission against this actual-harness receipt')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    controls=prepare(args.output/'controls',adversarial=True)
    # Known-positive attack control: the same clipped UI changes v1's verdict
    # only when its page-world overrides execute. V2 below must still reject it.
    historical={}
    for name in ('clipped-title','tampered-clipping'):
        report=grade_submission(controls/name,ORACLES['activity-explorer-v1'],args.image,timeout=120)
        (args.output/('historical-v1-'+name+'.json')).write_text(json.dumps(report,indent=2)+'\n')
        historical[name]=report['score']
    if historical != {'clipped-title':0.75,'tampered-clipping':1.0}:
        raise RuntimeError(f'known-positive v1 attack control did not activate: {historical}')
    targets={'broken-filter':'combined-filter','clipped-title':'layout-360','nested-scroll':'layout-360',
             'hidden-text':'layout-360','broken-retry':'retry','broken-keyboard':'keyboard','wide-detail':'detail-mobile',
             'reversed-list':'list', 'duplicate-record':'list', 'missing-visible-title':'layout-360',
             'truncated-description':'detail', 'tampered-clipping':'layout-360',
             'tampered-order':'list', 'tampered-description':'detail', 'tampered-keyboard':'keyboard',
             'invisible-body':['layout-360','detail','detail-mobile'],
             'invisible-html':['layout-360','detail','detail-mobile'],
             'faded-ancestors':['layout-360','detail','detail-mobile'],
             'invisible-details':['detail','detail-mobile']}
    def must_fail(name):
        value=targets.get(name,'search')
        return [value] if isinstance(value,str) else value
    summary=[]
    sources=[('baseline',ROOT/'benchmarks/harbor/local/activity-explorer-c2-o2/environment/app'),
             *[(p.name,p) for p in sorted(controls.iterdir())]]
    specification={'schema':1,'contract_review':'Synthetic activity explorer: static assets, semantic interactions, responsive content and keyboard access; preference unscored.',
                   'project_check':'pnpm build','controls':[
                       {'id':name,'role':'baseline' if name=='baseline' else 'valid' if name.startswith('valid-') else 'invalid',
                        'source':str(source.resolve()),'must_fail':[] if name.startswith('valid-') else must_fail(name)}
                       for name,source in sources]}
    spec_path=args.output/'controls.json'
    spec_path.write_text(json.dumps(specification,indent=2)+'\n')
    validated={}
    if args.workflow:
        from obench import repair_validation
        task=ROOT/'benchmarks/harbor/local/activity-explorer-c2-o2'
        receipt=repair_validation.validate(task,spec_path,args.image,args.workflow)
        receipt_path=args.output/'quality.json'
        receipt_path.write_text(json.dumps(receipt,indent=2)+'\n')
        if receipt['status']!='passed': raise RuntimeError(receipt['findings'])
        validated={record['id']:record['result']['grading'] for record in receipt['controls']}
    # Grade only the submitted web subtree, exactly as canonical source export.
    # With a workflow, validation just ran every real control. Reuse those
    # observations for reporting, then freshly replay admission below; avoid
    # a third identical execution of every control for screenshot export alone.
    import tempfile,shutil
    for name,source in sources:
        if args.workflow:
            report=validated[name]
        else:
            with tempfile.TemporaryDirectory() as temp:
                shutil.copytree(source/'web',Path(temp)/'web')
                report=grade_submission(Path(temp),ORACLES['activity-explorer-v2'],args.image,timeout=120)
        (args.output/(name+'.json')).write_text(json.dumps(report,indent=2)+'\n')
        failed=[c['case'] for c in report['checks'] if not c['pass']]
        for check in report['checks']:
            screenshot=check.get('observed',{}).get('value',{}).get('screenshot')
            if screenshot:
                (args.output/(name+'-'+check['case']+'.png')).write_bytes(base64.b64decode(screenshot,validate=True))
        if report.get('candidate_failure'):
            raise RuntimeError(f"{name}: incomplete observations: {report['candidate_failure']}")
        if name.startswith('valid-'):
            accepted=not failed
        elif name=='baseline':
            accepted=bool(failed)
        else:
            accepted=set(must_fail(name))<=set(failed) and len(failed)<len(report['checks'])
        if not accepted: raise RuntimeError(f'{name}: unexpected failed checks: {failed}')
        summary.append({'control':name,'score':report['score'],'failed':failed})
        print(json.dumps(summary[-1]),flush=True)
    if args.workflow:
        fresh=repair_validation.validate_receipt(receipt_path,task,args.image)
        (args.output/'admission-replay.json').write_text(json.dumps(fresh,indent=2)+'\n')
    (args.output/'summary.json').write_text(json.dumps({'passed':True,'image':args.image,'controls':summary,
        'oracle':'activity-explorer-v2','historical_attack_control':historical,
        'quality_admission_replayed':bool(args.workflow)},indent=2)+'\n')


if __name__=='__main__':main()
