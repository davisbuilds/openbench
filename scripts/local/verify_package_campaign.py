#!/usr/bin/env python3
"""Synthetic package campaign control: real harness, grader, fresh admission and Harbor lifecycle.

No inference, credentials or private authoring inputs. Outputs are development
control evidence, never model benchmark results.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'scripts/ci')]
from browser_quality_fixtures import prepare
from obench import evaluation_package, package_campaign, repair_validation, init, suite_run
from obench.repair_oracles.activity_explorer import cases


def prepare_control(output, image):
    controls = prepare(output/'controls', adversarial=True)
    source = output/'authoring'
    source.mkdir()
    descriptor = {'schema':1, 'id':'synthetic-package', 'case_revision':1, 'oracle_revision':1,
                  'split':'development', 'backend':'activity-explorer-v2'}
    requests = {name: request for name, _, request in cases()}
    detail = {**requests['detail'], 'data':requests['detail']['data'][:1]}
    data = [{'id':'list','bucket':'content','request':requests['list']},
            {'id':'detail','bucket':'detail','request':detail}]
    (source/'evaluation.json').write_text(json.dumps(descriptor))
    (source/'cases.json').write_text(json.dumps(data))
    (source/'evaluator.py').write_text('''from obench.repair_oracles.activity_explorer_v2 import reasons

def grade(cases, observations, fixtures):
    result = []
    for case, observed in zip(cases, observations, strict=True):
        failures = reasons(case['id'], observed.get('value', {}), case['request'])
        result.append({'pass':not failures,'reasons':failures})
    return result
''')
    baseline = ROOT/'benchmarks/harbor/local/activity-explorer-c2-o2'
    shutil.copytree(baseline/'environment/app', source/'solver/workspace')
    shutil.copyfile(baseline/'instruction.md', source/'solver/instruction.md')
    shutil.copytree(controls/'valid-list', source/'controls/reference')
    archive = output/'evaluation.tar'
    sha = evaluation_package.freeze(source, archive)
    auth = package_campaign.pin(archive, sha, True)
    project = output/'project'; project.mkdir()
    init.init_scaffold(project)
    tasks = project/'.openbench/tasks'; shutil.rmtree(tasks)
    task = tasks/'synthetic-package-c1-o1'
    package_campaign.export_task(auth.load(), task)
    spec = {'schema':1,'contract_review':'Synthetic two-check integration control; not difficulty evidence',
            'project_check':'pnpm build', 'controls':[
                {'id':'baseline','role':'baseline','source':str(task/'environment/app'),'must_fail':['detail']},
                *[{'id':name,'role':'valid','source':str(controls/name),'must_fail':[]} for name in ('valid-list','valid-cards')],
                {'id':'reversed-list','role':'invalid','source':str(controls/'reversed-list'),'must_fail':['list']},
                {'id':'truncated-description','role':'invalid','source':str(controls/'truncated-description'),'must_fail':['detail']} ]}
    spec_path = output/'controls.json'; spec_path.write_text(json.dumps(spec))
    suite = project/'.openbench/suites/default.toml'
    text = suite.read_text().replace('gpt-5.6-sol','gpt-6.1-sol-high')
    text += '\n[sandbox]\nkind="repair-v1"\nruntime_image='+json.dumps(image)+'\n'
    text += 'evaluation_archive='+json.dumps(str(archive))+'\nevaluation_sha256='+json.dumps(sha)+'\ntrust_evaluator=true\n'
    suite.write_text(text)
    return auth, task, spec_path, suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output=args.output.resolve(); output.mkdir(parents=True,exist_ok=False)
    auth, task, spec, suite = prepare_control(output, args.image)
    subprocess.run([sys.executable,str(ROOT/'scripts/local/verify_repair_codex.py'),
        '--runtime-image',args.image,'--task',str(task),'--model','gpt-6.1-sol-high',
        '--project-check','pnpm build','--evaluation-archive',str(auth.archive),
        '--evaluation-sha256',auth.sha256,'--trust-evaluator','--output-dir',str(output/'workflow')], check=True)
    report=repair_validation.validate(task,spec,args.image,output/'workflow/receipt.json',evaluation=auth)
    (output/'quality.json').write_text(json.dumps(report))
    if report['status'] != 'passed':
        raise RuntimeError(report['findings'])
    compiled=suite_run.compile_suite(suite)
    repair_validation.validate_campaign(compiled,[output/'quality.json'],evidence_dir=output/'admission')
    subprocess.run([sys.executable,str(ROOT/'scripts/local/verify_package_lifecycle.py'),
        '--runtime-image',args.image,'--task',str(task),'--archive',str(auth.archive),
        '--sha256',auth.sha256,'--trust-evaluator','--output',str(output/'lifecycle')],check=True)
    (output/'summary.json').write_text(json.dumps({'passed':True,'runtime_image':args.image,
        'package_sha256':auth.sha256,'model_calls':0,'fresh_quality_replay':True}))


if __name__ == '__main__':
    main()
