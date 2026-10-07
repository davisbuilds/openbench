#!/usr/bin/env python3
"""Real Docker/actual-Codex quality controls; offline, no credentials or inference."""
import argparse
import copy
import json
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from obench import repair_validation as rv, sandbox_grading as legacy
from obench.repair_evidence import Operation, status, write_json
from obench.repair_oracles import agentmonitor
from obench.repair_worker import run_worker
from obench.harbor_sandbox import read_tree
from repair_quality_fixtures import prepare


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def workflow(task, image, directory, command, *, success=True):
    process = subprocess.run([sys.executable, str(ROOT/'scripts/local/verify_repair_codex.py'),
        '--task', str(task), '--runtime-image', image, '--output-dir', str(directory),
        '--project-check', command], capture_output=True, timeout=300)
    (directory.parent/(directory.name+'.log')).write_bytes(process.stdout + process.stderr)
    require((process.returncode == 0) == success, 'unexpected workflow outcome; inspect ' + str(directory))
    return directory/'receipt.json'


def archive(files):
    with tempfile.TemporaryDirectory(prefix='quality-source-') as temporary:
        root = Path(temporary)
        for name, data in files.items():
            path = root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return legacy.source_archive(root, set(files))[0]


def seeded_cases(seed):
    rng = random.Random(seed)
    cases = []
    for index in range(8):
        plugin = f'/synthetic/.codex/plugins/cache/openai-curated-remote/package-{rng.randrange(10000)}/'
        left = plugin+'1.2.3/skills/review/SKILL.md'
        name = f'review-{rng.randrange(10000)}'
        count = rng.randrange(1, 5)
        original = [(name, left)]*count
        same = [(name, plugin+'9.8.7/skills/review/SKILL.md')]*count
        changed = [(name, plugin+'1.2.3/skills/other/SKILL.md')]*count
        for label, right, expected in [('version', same, None), ('skill', changed, (count, count, count, count)),
                                        ('duplicate', original + [(name, left)], (count, count+1, 0, 1))]:
            rng.shuffle(right)
            cases.append((f'{seed}:{index}:{label}', {'op': 'mismatch', 'live': legacy.block(original),
                         'recorded': [legacy.record('developer', legacy.block(right))]}, expected))
        outside = f'/synthetic/project-{index}/1.2.3/skills/review/SKILL.md'
        cases.append((f'{seed}:{index}:nonplugin', {'op': 'mismatch', 'live': legacy.block([(name, outside)]),
                     'recorded': [legacy.record('developer', legacy.block([(name, outside.replace('/1.2.3/', '/9.8.7/'))]))]},
                     (1, 1, 1, 1)))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to(ROOT/'results'), 'output must be under ignored results/')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    task, spec = prepare(ROOT, output)
    controls = output/'controls.json'
    write_json(controls, spec)
    receipt = workflow(task, args.runtime_image, output/'workflow', spec['project_check'])
    runs = []
    for iteration in range(2):
        with Operation(output/f'quality-{iteration}', 'quality-ci') as evidence:
            report = rv.validate(task, controls, args.runtime_image, receipt, evidence=evidence)
            evidence.finish(report)
        require(report['status'] == 'passed', str(report['findings']))
        runs.append(report)
    matrix = {check['id']: {record['id']: next(c['pass'] for c in record['result']['grading']['checks'] if c['id'] == check['id'])
                           for record in runs[0]['controls']} for check in runs[0]['task']['checks']}
    for first, second in zip(runs[0]['controls'], runs[1]['controls'], strict=True):
        require([c['pass'] for c in first['result']['grading']['checks']] ==
                [c['pass'] for c in second['result']['grading']['checks']], 'unstable named verdicts')
    quality = output/'quality.json'
    write_json(quality, runs[0])
    with Operation(output/'admission', 'admission-ci') as evidence:
        evidence.finish(rv.validate_receipt(quality, task, args.runtime_image, evidence=evidence))

    # A genuinely failed production control is rewritten to claim it passed;
    # hashes and specification stay consistent. Only fresh replay can reject it.
    failed_spec = copy.deepcopy(spec)
    broken = output/'broken-reference'
    shutil.copytree(output/'skill-defect', broken)
    with (broken/'scripts/profiles/rollout_codex.py').open('a') as stream:
        stream.write('\n# Separate invalid reference control.\n')
    failed_spec['controls'][1]['source'] = str(broken)
    bad_controls = output/'failed-controls.json'
    write_json(bad_controls, failed_spec)
    failed = rv.validate(task, bad_controls, args.runtime_image, receipt)
    require(failed['status'] == 'failed', 'negative receipt control did not fail')
    forged = copy.deepcopy(failed)
    forged.update(status='passed', findings=[])
    for record in forged['controls']:
        if record['role'] == 'valid':
            result = record['result']; result['solved'] = True
            result['grading']['score'] = 1
            result['grading']['buckets'] = dict.fromkeys(result['grading']['buckets'], True)
            for check in result['grading']['checks']: check['pass'] = True
    require(rv.assess_controls(forged['controls'], forged['task']) == [], 'forgery did not reach replay guard')
    forged_path = output/'forged.json'
    write_json(forged_path, forged)
    try:
        with Operation(output/'forged-admission', 'admission-ci') as evidence:
            evidence.finish(rv.validate_receipt(forged_path, task, args.runtime_image, evidence=evidence))
    except ValueError as exc:
        require('recomputed quality controls failed' in str(exc), 'forgery rejected by an unrelated guard: ' + str(exc))
    else:
        raise RuntimeError('forged receipt authorized admission')

    class Interrupted(Operation):
        def artifact(self, kind, value):
            result = super().artifact(kind, value)
            if kind == 'control': raise KeyboardInterrupt('controlled interruption after real worker cleanup')
            return result
    try:
        with Interrupted(output/'interrupted', 'quality-ci') as evidence:
            evidence.finish(rv.validate(task, controls, args.runtime_image, receipt, evidence=evidence))
    except KeyboardInterrupt:
        pass
    partial = status(output/'interrupted')
    require(partial['status'] == 'incomplete' and len(partial['evidence']) == 1, 'interruption evidence lost')

    negative = workflow(task, args.runtime_image, output/'failed-workflow', 'false && printf should-not-run', success=False)
    try:
        rv.validate_workflow(negative, rv.inspect_task(task), args.runtime_image, 'false && printf should-not-run')
    except ValueError:
        pass
    else:
        raise RuntimeError('failed workflow accepted')

    variation_reports = []
    for name in ('reference', 'alternative', 'skill-defect', 'version-defect'):
        files = rv.source_files(output/name, rv.inspect_task(task))
        for seed in (7, 41, 20261007):
            cases = seeded_cases(seed)
            observed, worker = legacy.run_worker(args.runtime_image, archive(files), [c[1] for c in cases])
            checks = [{'id': cid, 'pass': result['ok'] and legacy.comparison(result.get('value'), expected, oracle_version=5),
                       'observed': result, 'expected': expected} for (cid, _, expected), result in zip(cases, observed, strict=True)]
            require(all(c['pass'] for c in checks) if name in ('reference', 'alternative') else any(not c['pass'] for c in checks),
                    'seeded invariant polarity failed: ' + name)
            variation_reports.append({'source': name, 'seed': seed, 'worker': worker, 'checks': checks})
    # Exercise both exception protocols in real workers, including operation ID.
    files = rv.source_files(output/'reference', rv.inspect_task(task))
    python_errors, _ = legacy.run_worker(args.runtime_image, archive(files), [{'op': 'unknown-ci-operation'}])
    require(python_errors[0]['error']['operation'] == 'unknown-ci-operation', 'Python diagnostic missing')
    am_files = {'src/'+key: value for key, value in read_tree(ROOT/'benchmarks/harbor/local/am-benchmark-pr106-c2-o3/environment/app/src').items()}
    node_errors, _ = run_worker(args.runtime_image, archive(am_files),
                               [{'steps': [{'op': 'init'}, {'op': 'unknown-ci-operation'}]}], program=agentmonitor.worker_program())
    require(node_errors[0]['error']['operation'] == '1:unknown-ci-operation', 'Node diagnostic missing')
    write_json(output/'summary.json', {'status': 'passed', 'matrix': matrix, 'repetitions': 2,
               'seeded_variations': variation_reports, 'diagnostics': [python_errors, node_errors],
               'live_inference': False, 'scope': 'synthetic infrastructure controls; no task difficulty claim'})
    print(json.dumps({'status': 'passed', 'summary': str(output/'summary.json')}))


if __name__ == '__main__':
    main()
