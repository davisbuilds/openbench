"""Operator-side inspect, replay and quality controls for isolated repairs.

Candidate code is executed only by the production confined grader. Reports
contain hidden evaluation evidence: keep them outside solver inputs and public
results. A passing receipt proves its declared controls, not task completeness.
"""
from contextlib import nullcontext
import argparse
import hashlib
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import tomllib

from . import repair_identity, sandbox_grading as legacy
from .harbor_sandbox import read_tree, validate_image
from .repair_oracles import registry
from .repair_workflow import validate_workflow, workflow_command

ROOT = Path(__file__).resolve().parents[1]



def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _implementation_hashes():
    # The report consumer and producer must agree, including source extraction.
    files = ['repair_validation.py', 'sandbox_grading.py', 'repair_grading.py',
             'repair_worker.py', 'repair_identity.py', 'harbor_sandbox.py', 'repair_evidence.py', 'repair_workflow.py']
    files += [str(p.relative_to(ROOT / 'obench')) for p in (ROOT / 'obench/repair_oracles').glob('*.py')]
    return {name: sha(ROOT / 'obench' / name) for name in sorted(files)}


_LOADED_IMPLEMENTATION = _implementation_hashes()


def implementation():
    current = _implementation_hashes()
    if current != _LOADED_IMPLEMENTATION:
        raise ValueError('repair validation implementation changed after import')
    return current


def inspect_task(task):
    task = Path(task).resolve()
    metadata = tomllib.loads((task / 'task.toml').read_text())['metadata']
    revision = repair_identity.resolve(metadata)
    validate = registry.validate_task_binding if metadata.get('openbench_oracle') else legacy.validate_task_binding
    manifest = validate(task, metadata['openbench_task_content_digest'])
    if metadata.get('openbench_oracle'):
        from .repair_grading import oracle_module
        oracle = registry.select(metadata)
        cases = oracle_module(oracle).cases()
        checks = [{'id': name, 'bucket': bucket} for name, bucket, _ in cases]
        prefix = oracle.source_prefix.rstrip('/')
    else:
        version = revision['oracle_revision']
        if version == 6:
            from .repair_oracles.dojo_v6 import cases
            checks = [{'id': name, 'bucket': bucket} for name, bucket, _, _ in cases()]
        else:
            checks = [{'id': f'dojo-v{version}-{index:03}', 'bucket': bucket}
                      for index, (bucket, _, _) in enumerate(legacy.dojo_cases(oracle_version=version))]
        prefix = 'scripts/profiles'
    return {'schema': 1, 'kind': 'repair-task-inspection', 'task': str(task),
            'task_binding': metadata['openbench_task_content_digest'],
            'revision': repair_identity.record(manifest), 'source_prefix': prefix,
            'checks': checks, 'buckets': sorted({c['bucket'] for c in checks}),
            'quality_eligible': not ((revision['oracle'] == 'dojo-evidence' and revision['oracle_revision'] < 6)
                                     or (revision['oracle'] == 'activity-explorer' and revision['oracle_revision'] < 2)),
            'limitations': ['controls do not establish complete specification coverage or calibrated difficulty'],
            'next': 'obench repair validate TASK --controls CONTROLS.json --workflow WORKFLOW.json --image sha256:... --output RECEIPT.json'}


def source_files(source, info):
    """Read the permitted subtree, never import the submitted Python on host."""
    source = Path(source).absolute()
    prefix = Path(info['source_prefix'])
    if source.is_symlink() or any((source / p).is_symlink() for p in (prefix, *prefix.parents)):
        raise ValueError('source tree contains a symlink ancestor')
    files = {f'{prefix.as_posix()}/{name}': data for name, data in read_tree(source / prefix).items()}
    if info['revision']['oracle'] == 'dojo-evidence':
        expected = {p.relative_to(Path(info['task']) / 'environment/app').as_posix()
                    for p in (Path(info['task']) / 'environment/app' / prefix).glob('*.py')}
        # Non-source scratch files are deliberately outside the submission.
        files = {name: data for name, data in files.items() if name in expected}
        if set(files) != expected:
            raise ValueError('missing required Dojo source files')
    return files


def source_hashes(source, info):
    return {name: hashlib.sha256(data).hexdigest() for name, data in source_files(source, info).items()}


def replay(task, source, image):
    info = inspect_task(task)
    validate_image(image)
    files = source_files(source, info)
    with tempfile.TemporaryDirectory(prefix='obench-replay-') as tmp:
        for name, data in files.items():
            p = Path(tmp) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        if info['revision']['oracle'] == 'dojo-evidence':
            graded = legacy.grade_submission(tmp, set(files), image, timeout=90,
                                            oracle_version=info['revision']['oracle_revision'])
        else:
            from .repair_grading import grade_submission
            metadata = tomllib.loads((Path(task) / 'task.toml').read_text())['metadata']
            graded = grade_submission(tmp, registry.select(metadata), image)
    # Stable operator IDs for old oracles, without changing their verdicts.
    for index, check in enumerate(graded['checks']):
        check.setdefault('id', info['checks'][index]['id'])
    result = {'schema': 1, 'kind': 'repair-replay', 'status': 'completed',
              'task': info, 'source': str(Path(source).resolve()), 'image': image,
              'implementation': implementation(), 'source_sha256': source_hashes(source, info),
              'grading': graded, 'solved': graded['score'] == 1,
              'scope': 'offline artifact replay; no model attempt; original results unchanged'}
    if graded.get('source_sha256') is not None and graded['source_sha256'] != result['source_sha256']:
        raise ValueError('source changed during replay')
    if inspect_task(task) != info:
        raise ValueError('task changed during replay')
    return result


def assess_controls(records, info):
    """Check actual observations rather than trusting a supplied passed flag."""
    findings = []
    accepted = [r for r in records if r['role'] == 'valid']
    if len(accepted) < 2:
        findings.append('at least two distinct valid repairs are required')
    if sum(r['role'] == 'baseline' for r in records) != 1:
        findings.append('exactly one untouched baseline is required')
    hashes = [fingerprint(r['result']['source_sha256']) for r in records]
    if len(hashes) != len(set(hashes)):
        findings.append('duplicate source controls do not establish distinct repairs')
    killed, buckets = set(), set()
    ids = {c['id']: c['bucket'] for c in info['checks']}
    for r in records:
        result = r['result']
        grading = result['grading']
        checks = grading['checks']
        if (grading.get('candidate_failure') or {c['id'] for c in checks} != set(ids)
                or len(checks) != len(ids) or any(type(c.get('pass')) is not bool for c in checks)
                or type(result.get('solved')) is not bool
                or result['solved'] != (grading['score'] == 1)
                or result['solved'] != all(c.get('pass') is True for c in checks)):
            findings.append(f"{r['id']}: incomplete observations or invalid candidate; cannot serve as a quality control")
            continue
        failed = {c['id'] for c in checks if c['pass'] is False}
        if r['role'] == 'valid':
            if failed or not result['solved']:
                findings.append(f"{r['id']}: valid repair rejected: {', '.join(sorted(failed))}")
        else:
            if r['role'] == 'invalid' and not any(c['pass'] for c in checks):
                findings.append(f"{r['id']}: defective control must retain a passing invariant; wholesale failure cannot establish a targeted defect")
                continue
            targets = set(r['must_fail'])
            if not targets or not targets <= set(ids):
                findings.append(f"{r['id']}: must_fail must name existing checks")
            elif not targets <= failed:
                findings.append(f"{r['id']}: intended defect escaped: {', '.join(sorted(targets - failed))}")
            elif r['role'] == 'invalid':
                killed |= targets
                buckets |= {ids[c] for c in targets}
            if result['solved']:
                findings.append(f"{r['id']}: defective repair accepted")
    if buckets != set(info['buckets']):
        findings.append('targeted defective controls must cover every scoring bucket')
    if info['revision']['oracle'] == 'dojo-evidence':
        required = {'different-skill-file', 'nonplugin-version-directory'}
        if not required <= killed:
            findings.append('Dojo requires source-identity boundary defect controls')
    if not info['quality_eligible']:
        findings.append('historical Dojo oracle is quarantined; use v6 with a reviewed case contract'
                        if info['revision']['oracle'] == 'dojo-evidence' else
                        'historical browser oracle is quarantined; use activity-explorer-c2-o2 with protected observations')
    return findings



def validate(task, controls, image, workflow, *, evidence=None):
    info = inspect_task(task)
    controls = Path(controls).resolve()
    raw = controls.read_bytes()
    spec = json.loads(raw)
    if (not isinstance(spec, dict) or set(spec) != {'schema', 'contract_review', 'project_check', 'controls'}
            or type(spec['schema']) is not int or spec['schema'] != 1
            or not isinstance(spec['contract_review'], str) or not spec['contract_review'].strip()
            or not isinstance(spec['controls'], list) or not spec['controls']):
        raise ValueError('controls require schema=1, contract_review notes, project_check and a nonempty controls array')
    with evidence.stage('workflow') if evidence else nullcontext():
        validate_workflow(workflow, info, image, spec['project_check'])
    records, names = [], set()
    baseline = Path(task).resolve() / 'environment/app'
    for control in spec['controls']:
        if (not isinstance(control, dict) or set(control) != {'id', 'role', 'source', 'must_fail'}
                or not isinstance(control['id'], str) or not control['id'] or control['id'] in names
                or control['role'] not in ('baseline', 'valid', 'invalid')
                or not isinstance(control['source'], str)
                or not isinstance(control['must_fail'], list)
                or any(not isinstance(x, str) for x in control['must_fail'])
                or (control['role'] == 'valid' and control['must_fail'])):
            raise ValueError('invalid or duplicate control; require id, role, source, must_fail')
        names.add(control['id'])
        source = (controls.parent / control['source']).resolve()
        if control['role'] == 'baseline' and source != baseline:
            raise ValueError('baseline must be the task environment/app, not a substitute')
        with evidence.stage('control:' + control['id']) if evidence else nullcontext():
            record = {**control, 'source': str(source), 'result': replay(task, source, image)}
            if evidence:
                evidence.artifact('control', record)
            records.append(record)
    if controls.read_bytes() != raw:
        raise ValueError('control specification changed during validation')
    findings = assess_controls(records, info)
    return {'schema': 1, 'kind': 'repair-quality', 'status': 'failed' if findings else 'passed',
            'task': info, 'image': image, 'implementation': implementation(),
            'controls_path': str(controls), 'controls_sha256': hashlib.sha256(raw).hexdigest(),
            'contract_review': spec['contract_review'], 'controls': records, 'findings': findings,
            'workflow': {'path': str(Path(workflow).resolve()), 'sha256': sha(workflow), 'project_check': spec['project_check']},
            'scope': 'declared repair controls only; workflow admission and difficulty calibration are separate'}


def validate_receipt(path, task, image, *, evidence=None):
    try:
        return _validate_receipt(path, task, image, evidence=evidence)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('malformed repair quality receipt') from exc


def _validate_receipt(path, task, image, *, evidence=None):
    value = json.loads(Path(path).read_text())
    info = inspect_task(task)
    if (value.get('schema') != 1 or value.get('kind') != 'repair-quality'
            or value.get('status') != 'passed' or value.get('task') != info
            or value.get('image') != image or value.get('implementation') != implementation()
            or sha(value['controls_path']) != value['controls_sha256']):
        raise ValueError('missing, failed or stale repair quality receipt')
    for record in value['controls']:
        result = record['result']
        if (result['task'] != info or result['image'] != image or result['implementation'] != implementation()
                or source_hashes(record['source'], info) != result['source_sha256']):
            raise ValueError('quality control source or treatment changed')
    workflow = value['workflow']
    if sha(workflow['path']) != workflow['sha256']:
        raise ValueError('workflow evidence changed')
    validate_workflow(workflow['path'], info, image, workflow['project_check'])
    if assess_controls(value['controls'], info):
        raise ValueError('quality receipt has failing or incomplete controls')
    # Hashing source bytes cannot authenticate claims about their behavior.
    # Reconstruct controls from the bound specification and use the production
    # confined grader again; saved pass/score fields never authorize dispatch.
    fresh = validate(task, value['controls_path'], image, workflow['path'], evidence=evidence)
    if evidence:
        evidence.artifact('recomputed', fresh)
    if fresh['status'] != 'passed':
        raise ValueError('recomputed quality controls failed: ' + '; '.join(fresh['findings']))
    if (fresh['controls_sha256'] != value['controls_sha256']
            or fresh['task'] != info or fresh['workflow'] != workflow):
        raise ValueError('quality specification or workflow changed during admission')
    def declared(records):
        return [{key: record[key] for key in ('id', 'role', 'source', 'must_fail')}
                for record in records]
    if declared(fresh['controls']) != declared(value['controls']):
        raise ValueError('quality controls differ from the bound specification')
    for saved, current in zip(value['controls'], fresh['controls'], strict=True):
        # Raw observations include fresh worker paths/timestamps. Compare the
        # semantic verdicts and return newly derived observations, never the
        # stored ones. Do not normalize candidate-controlled strings.
        def verdict(record):
            result = record['result']
            grading = result['grading']
            return {'solved': result['solved'], 'source_sha256': result['source_sha256'],
                    'score': grading['score'], 'buckets': grading.get('buckets'),
                    'candidate_failure': grading.get('candidate_failure'),
                    'checks': [{k: check[k] for k in ('id', 'bucket', 'pass')}
                               for check in grading['checks']]}
        if fingerprint(verdict(saved)) != fingerprint(verdict(current)):
            raise ValueError('saved verdict differs from recomputed grading observations')
    return fresh


def validate_campaign(compiled, paths, *, evidence_dir=None):
    if evidence_dir is not None:
        from .repair_evidence import Operation
        with Operation(evidence_dir, "campaign-admission") as evidence:
            result = _validate_campaign(compiled, paths, evidence=evidence)
            evidence.finish(result)
            return result
    return _validate_campaign(compiled, paths)


def _validate_campaign(compiled, paths, *, evidence=None):
    tasks = [group.task_set.path / name for group in compiled.task_sets for name in group.task_names]
    by_task = {}
    for path in paths:
        value = json.loads(Path(path).read_text())
        try:
            task = Path(value['task']['task']).resolve()
        except (KeyError, TypeError) as exc:
            raise ValueError('malformed repair quality receipt: missing task identity') from exc
        if task in by_task:
            raise ValueError('duplicate quality receipt for task')
        by_task[task] = path
    if set(by_task) != {p.resolve() for p in tasks}:
        raise ValueError('every repair task requires exactly one --quality receipt from obench repair validate')
    reports = []
    for task in tasks:
        with evidence.stage('task:' + str(task)) if evidence else nullcontext():
            result = validate_receipt(by_task[task.resolve()], task, compiled.suite.sandbox.runtime_image, evidence=evidence)
            reports.append(result)
    return {'schema': 1, 'kind': 'repair-campaign-admission', 'status': 'passed', 'tasks': reports}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='obench repair', description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('inspect', 'replay', 'validate'):
        p = sub.add_parser(action)
        p.add_argument('task', type=Path)
        p.add_argument('--json', action='store_true', help='one structured operator report on stdout')
        if action != 'inspect':
            p.add_argument('--image', required=True, help='immutable sha256 runtime identity')
            p.add_argument('--output', type=Path, help='create private JSON evidence; refuses overwrite')
            p.add_argument('--evidence-dir', type=Path, help='fresh private operation directory; defaults to OUTPUT.evidence')
        if action == 'replay':
            p.add_argument('--source', type=Path, required=True, help='frozen source or checkout root')
        if action == 'validate':
            p.add_argument('--controls', type=Path, required=True, help='operator-only control specification JSON')
            p.add_argument('--workflow', type=Path, required=True, help='actual-harness offline workflow receipt for this task/image')
    p = sub.add_parser('status', help='inspect a private operation, including interrupted attempts')
    p.add_argument('directory', type=Path)
    p.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        if getattr(args, 'output', None) and args.output.exists():
            raise ValueError('output exists; choose a fresh evidence path')
        from .repair_evidence import Operation, status, write_json
        def run(evidence=None):
            if args.action == 'status':
                return status(args.directory)
            if args.action == 'inspect':
                return inspect_task(args.task)
            if args.action == 'replay':
                with evidence.stage('replay') if evidence else nullcontext():
                    return replay(args.task, args.source, args.image)
            return validate(args.task, args.controls, args.image, args.workflow, evidence=evidence)
        directory = getattr(args, 'evidence_dir', None)
        if directory is None and getattr(args, 'output', None):
            directory = Path(str(args.output) + '.evidence')
        if directory:
            with Operation(directory, args.action) as evidence:
                result = run(evidence)
                if getattr(args, 'output', None):
                    write_json(args.output, result, exclusive=True)
                evidence.finish(result)
        else:
            result = run()
        if args.json:
            print(json.dumps(result, sort_keys=True))
        elif args.action == 'status':
            print(f"{result['status']}: {result['directory']} ({len(result['evidence'])} artifacts)")
            for blocker in result['blockers']:
                print('  ' + json.dumps(blocker))
        elif args.action == 'inspect':
            print(f"{result['revision']}: {len(result['checks'])} checks; quality eligible={result['quality_eligible']}")
            for check in result['checks']:
                print(f"  {check['bucket']}: {check['id']}")
            print(result['next'])
        elif args.action == 'replay':
            g = result['grading']
            print(f"Replay completed: score={g['score']}; solved={result['solved']}; original scores unchanged")
            for check in g['checks']:
                if not check['pass']:
                    print(f"  FAIL {check['id']} ({check['bucket']})")
        else:
            print(f"Quality controls: {result['status']}")
            for finding in result['findings']:
                print('  ' + finding)
            print(result['scope'])
        if result.get('status') in ('incomplete', 'interrupted', 'evidence_invalid', 'running'):
            return 2
        return 1 if (result.get('status') == 'failed' or result.get('solved') is False) else 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        if args.json:
            print(json.dumps({'schema': 1, 'status': 'incomplete', 'error': str(exc),
                              'evidence_dir': str(directory) if 'directory' in locals() and directory else None}))
        else:
            print(f'repair validation incomplete: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
