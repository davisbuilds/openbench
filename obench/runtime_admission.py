"""Local runtime admission produced by controls, consumed before campaign dispatch.

This is evidence for a trusted operator, not a signature against that operator.
Solver containers cannot read or write these host-side records.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import socket
import subprocess
import tempfile

from . import init, suite_run
from .harbor_run import preflight_harbor_binary

ROOT = Path(__file__).resolve().parents[1]
MARKER = b'# OPENBENCH_RUNTIME_CONTROL_OK\n'
CONTROL_TARGET = 'scripts/profiles/__init__.py'
SCRIPTS = (
    'scripts/local/verify_repair_sandbox.py',
    'scripts/local/verify_repair_codex.py',
    'scripts/ci/verify_sandbox_timeouts.py',
    'scripts/local/verify_repair_lifecycle.py',
    'scripts/local/verify_repair_log_export.py',
    'scripts/local/verify_repair_trajectory.py',
    'scripts/local/verify_registered_repair.py',
    'scripts/local/verify_registered_lifecycle.py',
    'scripts/local/verify_repair_developer.py',
)

CONTROLS = (*SCRIPTS, "runtime-sockets")


class AdmissionError(ValueError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(compiled, harbor_binary):
    if compiled.suite.sandbox is None:
        raise AdmissionError('runtime admission currently supports repair-v1 suites')
    if compiled.suite.run.concurrency != 1:
        raise AdmissionError('repair campaign admission currently supports serial trials')
    harbor = preflight_harbor_binary(harbor_binary)
    suite_run._verify_sandbox_runtime(compiled, harbor, run_process=subprocess.run)
    image = json.loads(subprocess.check_output(['docker','image','inspect',compiled.suite.sandbox.runtime_image],text=True))[0]
    daemon = json.loads(subprocess.check_output(['docker','info','--format','{{json .}}'],text=True))
    files = [p for p in (ROOT/'obench').rglob('*.py') if 'tests' not in p.relative_to(ROOT).parts]
    files += [ROOT/p for p in SCRIPTS] + [ROOT/'docker/repair-sandbox/Dockerfile', ROOT/'obench/tests/test_sandbox_gateway.py']
    files += list((ROOT/'docker/repair-sandbox/node').glob('*.json'))
    for control_root in ('benchmarks/harbor/local/dojo-evidence-pr60-v5',
                         'benchmarks/harbor/local/am-benchmark-pr106-v4',
                         'benchmarks/local/am-benchmark-pr106-v2',
                         'benchmarks/harbor/local/dojo-evidence-pr60-c3-o5',
                         'benchmarks/harbor/local/am-benchmark-pr106-c2-o3'):
        files += [p for p in (ROOT/control_root).rglob('*') if p.is_file() and '__pycache__' not in p.parts]
    return {'schema':1, 'host':socket.gethostname(),
            'image':{'id':image['Id'],'requested':compiled.suite.sandbox.runtime_image,'os':image['Os'],'architecture':image['Architecture']},
            'docker':{key:daemon.get(key) for key in ('ID','ServerVersion','OperatingSystem','Architecture')},
            'harbor':{'version':harbor.version,'commit':harbor.git_commit},
            'implementation':{str(p.relative_to(ROOT)):digest(p) for p in sorted(files)},
            'models':sorted({(arm.arm.model,arm.agent.model_name,arm.agent.kwargs['reasoning_effort']) for arm in compiled.arms}),
            'concurrency':1}


def canonical(value):
    return json.loads(json.dumps(value,sort_keys=True,allow_nan=False))


def model_control_records(expected):
    """One recorded offline tool loop per selected model/effort identity."""
    from .codex_models import REPAIR_MODEL_PAIRS
    models = expected.get('models')
    if (not isinstance(models, (list, tuple)) or not models
            or any(not isinstance(item, (list, tuple)) or len(item) != 3
                   or not all(isinstance(part, str) for part in item)
                   or REPAIR_MODEL_PAIRS.get(item[0]) != tuple(item[1:]) for item in models)):
        raise AdmissionError('invalid selected model identities for offline controls')
    if len({item[0] for item in models}) != len(models):
        raise AdmissionError('duplicate selected model identity for offline controls')
    return {alias: {
        'model': model, 'effort': effort,
        'log': 'offline-1.log' if index == 0 else f'model-{index}.log',
        'receipt': ('tool-loop' if index == 0 else f'tool-loop-{index}') + '/receipt.json',
    } for index, (alias, model, effort) in enumerate(models)}


def validate_model_controls(directory, expected, records, evidence=None):
    from .harbor_agents.sandbox_codex import CLI_VERSION
    required = model_control_records(expected)
    if not required or records != required:
        raise AdmissionError('every selected model requires an offline tool-loop control')
    for alias, record in required.items():
        if evidence is not None and any(record[key] not in evidence for key in ('log', 'receipt')):
            raise AdmissionError('model control evidence is not bound')
        try:
            receipt = json.loads((Path(directory) / record['receipt']).read_text())
        except (OSError, ValueError) as exc:
            raise AdmissionError('model control receipt is unavailable or invalid') from exc
        if not isinstance(receipt, dict):
            raise AdmissionError('model control receipt must be an object')
        exact = {'status': 'passed', 'cleanup_confirmed': True, 'live_inference': False,
                 'real_credentials': False, 'model_alias': alias, 'model': record['model'],
                 'effort': record['effort'], 'runtime_image': expected['image']['requested'],
                 'codex_version': CLI_VERSION, 'probe_sha256': expected['implementation'][SCRIPTS[1]],
                 'actual_tool_mutation': True, 'tool_result_returned': True,
                 'final_response_present': True, 'request_count': 2}
        exact['developer_workflows_passed'] = True
        if any(type(receipt.get(key)) is not type(value) or receipt.get(key) != value for key, value in exact.items()):
            raise AdmissionError('model control used another or incomplete execution treatment')


def validate_admission(path, expected):
    """Re-read immutable control artifacts, then compare current runtime inputs."""
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('kind') != 'repair-runtime-admission' or value.get('passed') is not True:
        raise AdmissionError('missing passing runtime admission')
    if value.get('fingerprint') != canonical(expected):
        raise AdmissionError('runtime admission is stale or belongs to another execution treatment')
    evidence = value.get('evidence')
    if not isinstance(evidence, dict) or not evidence:
        raise AdmissionError('runtime admission lacks control evidence')
    for relative, expected_hash in evidence.items():
        candidate = path.parent / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or candidate.is_symlink() or not candidate.resolve().is_relative_to(path.parent) or not candidate.is_file():
            raise AdmissionError('invalid admission evidence path')
        if digest(candidate) != expected_hash:
            raise AdmissionError('runtime admission evidence changed')
    control = value.get('authenticated_control')
    if not isinstance(control, str) or control not in evidence:
        raise AdmissionError('authenticated control must be bound into evidence')
    required = {script: {'exit_code':0, 'log':f'offline-{index}.log'} for index,script in enumerate(CONTROLS)}
    if value.get('offline_controls') != required or any(item['log'] not in evidence for item in required.values()):
        raise AdmissionError('every required offline control must have passing evidence')
    boundary_path = path.parent / 'boundary.json'
    if 'boundary.json' not in evidence:
        raise AdmissionError('missing boundary control receipt')
    boundary = json.loads(boundary_path.read_text())
    if boundary.get('runtime_image') != expected['image']['requested'] or boundary.get('probe_sha256') != expected['implementation'][SCRIPTS[0]]:
        raise AdmissionError('boundary control does not bind the admitted runtime')
    # The control's normal importer and manifest verifier remain authoritative.
    manifest = path.parent / value['authenticated_control']
    suite_run.verify_suite_run(manifest)
    treatment = json.loads(manifest.read_text())['suite_manifest']
    if (treatment.get('sandbox', {}).get('runtime_image') != expected['image']['requested']
            or sorted({arm['canonical_model'] for arm in treatment['arms']}) != sorted({m[0] for m in expected['models']})
            or treatment['run']['attempts'] != 1 or treatment['run']['max_retries'] != 0
            or treatment['run']['concurrency'] != 1 or treatment['run']['timeout_seconds'] != 180):
        raise AdmissionError('authenticated control used another execution treatment')
    validate_model_controls(path.parent, expected, value.get('model_controls'), evidence)
    return value



def control_edit(task):
    import tomllib
    metadata=tomllib.loads((Path(task)/'task.toml').read_text()).get('metadata',{})
    if metadata.get('openbench_oracle') in ('agentmonitor-benchmark-v2', 'agentmonitor-benchmark-v3'):
        return 'src/db/schema.ts', b'// OPENBENCH_RUNTIME_CONTROL_OK\n'
    return CONTROL_TARGET, MARKER


def prepare_control(compiled, directory):
    """A bounded file-edit control, kept outside the benchmark task identity."""
    if len(compiled.task_sets) != 1:
        raise AdmissionError('qualification currently requires one repair task set')
    selected = compiled.task_sets[0]
    # Runtime qualification is independent of the requested repair's oracle.
    # Compilation already validates that target's trusted task binding.
    directory.mkdir(parents=True, exist_ok=False)
    init.init_scaffold(directory)
    tasks = directory/'.openbench/tasks'
    shutil.rmtree(tasks)
    source = selected.task_set.path/selected.task_names[0]
    task = tasks/source.name
    shutil.copytree(source,task)
    target,marker=control_edit(task)
    append_command = "printf '%s\\n' " + shlex.quote(marker.decode().strip()) + " >> " + shlex.quote('/app/'+target)
    (task/'instruction.md').write_text(
        "This is a file-edit control. Run this shell command to append one comment "
        "at the absolute end of the file, after all existing content:\n\n"
        +append_command+"\n\nLeave all existing bytes and every other file unchanged. "
        "Reply 'done' and stop. This is not a repair challenge.\n")
    from .sandbox_grading import task_digest as legacy_digest
    from .repair_oracles.registry import task_digest as registered_digest
    config = task/'task.toml'
    digest_fn=registered_digest if target.startswith('src/') else legacy_digest
    text = re.sub(r'(\[metadata.openbench_task_content_digest\]\nscheme = [34]\nsha256 = ")[a-f0-9]{64}',
                  lambda m:m.group(1)+digest_fn(task),config.read_text())
    config.write_text(text)
    suite = directory/'.openbench/suites/default.toml'
    text = init.DEFAULT_SUITE_TOML.replace('private-default','runtime-control')
    start,end=text.index('[[arms]]'),text.index('[run]')
    arms=''.join('[[arms]]\nid = '+json.dumps(arm.arm.id)+'\nharness = "codex"\nprofile = "local-codex"\nmodel = '+json.dumps(arm.arm.model)+'\n\n' for arm in compiled.arms)
    text=text[:start]+arms+text[end:]
    text=re.sub(r'^timeout_seconds = .*$', 'timeout_seconds = 180',text,flags=re.M)
    text+='\n[sandbox]\nkind="repair-v1"\nruntime_image='+json.dumps(compiled.suite.sandbox.runtime_image)+'\nmax_requests=20\n'
    suite.write_text(text)
    control=suite_run.compile_suite(suite)
    return control, task


def verify_control(control, task, result_path):
    """Check actual edits and production stream evidence after canonical import."""
    rows=[json.loads(line) for line in Path(result_path).read_text().splitlines()]
    from .stats import validate_suite_rows
    validate_suite_rows(rows)
    if len(rows)!=len(control.arms) or any(r.get('completed') is not True or r.get('error') for r in rows):
        raise AdmissionError('authenticated control did not complete every arm')
    jobs={job.task_set_id:Path(control.config.jobs_dir)/job.artifact.job_name for job in suite_run.plan_jobs(control)}
    for row in rows:
        p=row['candidate_provenance']
        trial=jobs[p['suite_task_set_id']]/p['harbor_trial_name']
        receipt=json.loads((trial/'verifier/sandbox-grading.json').read_text())
        from .harbor_sandbox import read_tree, source_receipt
        expected=read_tree(task/'environment/app')
        target,marker=control_edit(task)
        expected[target]+=marker
        if receipt['freeze'].get('workspace_files') != source_receipt(expected)['files']:
            raise AdmissionError('authenticated control changed workspace beyond the requested edit')
        events=[json.loads(line) for line in (trial/'verifier/sandbox-gateway.jsonl').read_text().splitlines()]
        complete=[e for e in events if e.get('event')=='request' and e.get('outcome')=='complete']
        if not complete:
            raise AdmissionError('authenticated control has no completed production stream')
        for event in complete:
            peer=event.get('upstream_peer') or {}
            if event.get('upstream_status')!=200 or peer.get('port')!=443 or not ipaddress.ip_address(peer.get('ip','')).is_global:
                raise AdmissionError('authenticated control lacks production upstream evidence')
    return rows


def qualify(compiled, directory, harbor_binary, auth_file):
    """Run fresh offline controls, then explicit local OAuth file-edit controls."""
    from .campaign import write_record, stamp
    directory=Path(directory)
    before=fingerprint(compiled,harbor_binary)
    harbor=preflight_harbor_binary(harbor_binary)
    python=str(suite_run._harbor_python_interpreter(harbor))
    image=compiled.suite.sandbox.runtime_image
    task=ROOT/'benchmarks/harbor/local/dojo-evidence-pr60-v5'
    model_records = model_control_records(before)
    first_alias = next(iter(model_records))
    commands=[
        [python,SCRIPTS[0],'--runtime-image',image,'--task',str(task),'--receipt',str(directory/'boundary.json')],
        [python,SCRIPTS[1],'--runtime-image',image,'--task',str(task),'--model',first_alias,'--output-dir',str(directory/'tool-loop')],
        [python,SCRIPTS[2],'--runtime-image',image,'--task',str(task),'--output',str(directory/'timeouts')],
        [python,SCRIPTS[3],'--runtime-image',image,'--task',str(task),'--reference',str(ROOT/'benchmarks/local/dojo-evidence-pr60/solution'),'--output',str(directory/'lifecycle')],
        [python,SCRIPTS[4],'--runtime-image',image,'--task',str(task),'--output',str(directory/'log-export')],
        [python,SCRIPTS[5],'--output',str(directory/'trajectory')],
        [python,SCRIPTS[6],'--runtime-image',image,'--output',str(directory/'registered-oracle')],
        [python,SCRIPTS[7],'--runtime-image',image,'--output',str(directory/'registered-lifecycle')],
        [python,SCRIPTS[8],'--runtime-image',image,'--output',str(directory/'developer-workflows')],
        ['docker','run','--rm','--network','none','--cap-drop','ALL','--security-opt','no-new-privileges',
         '--user','10001:10001','-i',image,'python3','-','-v'],
    ]
    for index, command in enumerate(commands):
        with (directory/f'offline-{index}.log').open('x') as log:
            if index == len(SCRIPTS):
                with (ROOT/'obench/tests/test_sandbox_gateway.py').open('rb') as source:
                    subprocess.run(command,cwd=ROOT,check=True,stdin=source,stdout=log,stderr=subprocess.STDOUT)
            else:
                subprocess.run(command,cwd=ROOT,check=True,stdout=log,stderr=subprocess.STDOUT)
    for alias, record in list(model_records.items())[1:]:
        with (directory/record['log']).open('x') as log:
            subprocess.run([python,SCRIPTS[1],'--runtime-image',image,'--task',str(task),
                            '--model',alias,'--output-dir',str((directory/record['receipt']).parent)],
                           cwd=ROOT,check=True,stdout=log,stderr=subprocess.STDOUT)
    validate_model_controls(directory, before, model_records)
    if canonical(before) != canonical(fingerprint(compiled,harbor_binary)):
        raise AdmissionError('runtime changed during offline controls; authentication was not read')
    control,control_task=prepare_control(compiled,directory/'control')
    write_record(directory/'control-jobs.json', {'schema':1, 'jobs':[str(Path(control.config.jobs_dir)/job.artifact.job_name) for job in suite_run.plan_jobs(control)]})
    # Authentication is read only after all offline controls pass. File bytes
    # stay in a private temporary HOME; never hash or copy them into receipts.
    auth_file=Path(auth_file).expanduser()
    if auth_file.is_symlink() or not auth_file.is_file():
        raise AdmissionError('explicit local OAuth file is unavailable')
    with tempfile.TemporaryDirectory(prefix='obench-control-home-') as home:
        home=Path(home)
        (home/'.codex').mkdir(mode=0o700)
        shutil.copyfile(auth_file,home/'.codex/auth.json')
        (home/'.codex/auth.json').chmod(0o600)
        env=dict(os.environ,HOME=str(home),CODEX_HOME=str(home/'.codex'),
                 DOCKER_CONFIG=os.environ.get('DOCKER_CONFIG',str(Path.home()/'.docker')))
        for key in ('OPENAI_API_KEY','OPENAI_BASE_URL','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL'):
            env.pop(key,None)
        with (directory/'authenticated-control.log').open('x') as log:
            subprocess.run([python,'-m','obench','run',str(control.suite.path),'--harbor-binary',harbor_binary],
                           cwd=ROOT,env=env,check=True,stdout=log,stderr=subprocess.STDOUT)
    run_dir=Path(control.config.results_dir)/'suite-runs'
    manifest=run_dir/(control.manifest_sha256+'.run.json')
    verified=suite_run.verify_suite_run(manifest)
    verify_control(control,control_task,verified['results_path'])
    after=fingerprint(compiled,harbor_binary)
    if canonical(before)!=canonical(after):
        raise AdmissionError('runtime changed while qualification was running')
    files=[p for p in directory.rglob('*') if p.is_file() and p.name not in ('execution.lock','campaign.log','worker.json','launch.json')]
    if any(p.is_symlink() or p.name == 'auth.json' for p in files):
        raise AdmissionError('unexpected link or credential artifact in control evidence')
    receipt={'schema':1,'kind':'repair-runtime-admission','passed':True,'created_at':stamp(),
             'fingerprint':canonical(after),'offline_controls':{script:{'exit_code':0,'log':f'offline-{index}.log'} for index,script in enumerate(CONTROLS)},
             'model_controls':model_records,
             'authenticated_control':str(manifest.relative_to(directory)),
             'evidence':{str(p.relative_to(directory)):digest(p) for p in files}}
    destination=directory/'admission.json'
    write_record(destination,receipt)
    validate_admission(destination,after)
    return destination
