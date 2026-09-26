#!/usr/bin/env python3
"""Offline real-container controls for the registered AgentMonitor oracle."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from obench.harbor_sandbox import read_tree
from obench.repair_grading import grade_submission
from obench.repair_oracles.registry import ORACLES


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    task=ROOT/'tasks-local/am-benchmark-pr106-v2'
    spec=importlib.util.spec_from_file_location('am106_control_variants',task/'validate_controls.py')
    variants_module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(variants_module)
    files=variants_module.FILES
    variants={'buggy':([],0.0),'reference':(files,1.0),'identity-only':(files[:2],.3333),
              'migration-only':([files[2]],.3333),'coverage-only':([files[3]],.3333),
              'identity-coverage':(files[:2]+[files[3]],.6667),
              **{name:(files,1.0) for name in ('absolute-identity','startup-migration','summary-and-session','alternative-combined')}}
    summary=[]
    for name,(overlay,expected) in variants.items():
        with tempfile.TemporaryDirectory(prefix='obench-am106-control-') as temporary:
            workspace=Path(temporary)
            shutil.copytree(task/'workspace/src',workspace/'src')
            for relative in overlay: shutil.copyfile(task/'solution'/relative,workspace/relative)
            variants_module.alternate(workspace,name)
            graded=grade_submission(workspace,ORACLES['agentmonitor-benchmark-v2'],args.runtime_image)
            (args.output/(name+'.json')).write_text(json.dumps(graded,indent=2)+'\n')
            observed=graded['score']
            record={'variant':name,'expected':expected,'score':observed,'buckets':graded['buckets']}
            summary.append(record)
            print(json.dumps(record),flush=True)
            if observed!=expected: raise RuntimeError(f'{name}: oracle control failed; inspect {args.output}')
    (args.output/'summary.json').write_text(json.dumps({'status':'passed','runtime_image':args.runtime_image,'controls':summary},indent=2)+'\n')


if __name__=='__main__': main()
