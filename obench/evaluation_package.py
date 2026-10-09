"""Frozen private evaluation packages and diagnostic replay, not campaign admission.

Evaluator Python is operator-trusted host code. Only candidate source and case
requests reach the closed public worker; private verdict code stays on the host.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile

from . import frozen_context, repair_worker
from .harbor_sandbox import pack_files, read_tree, validate_image
from .repair_evidence import Operation, write_json
from .repair_oracles import activity_explorer_v2
from .sandbox_grading import CandidateFailure

# Code paths are owned by OpenBench, never by the package descriptor.
BACKENDS = {'activity-explorer-v2': activity_explorer_v2}
REQUIRED = {'evaluation.json', 'cases.json', 'evaluator.py', 'solver/instruction.md'}
SCOPE = 'diagnostic private-package replay; not campaign admission or a benchmark result'


def _json(raw):
    return repair_worker.strict_json(raw)


def _browser_request(request):
    """Validate author-controlled inputs before any candidate operation runs.

The v2 worker addresses records by title and detail/keyboard by the first row.
Those protocol assumptions are different from arbitrary application test data.
"""
    required = {'mode', 'data'}
    allowed = required | {'viewport', 'query', 'status', 'largeText'}
    if not required <= request.keys() or not request.keys() <= allowed:
        raise ValueError('browser request requires mode/data and known optional fields')
    mode, rows = request['mode'], request['data']
    if mode not in ('list', 'loading', 'retry', 'detail', 'keyboard', 'layout') or not isinstance(rows, list):
        raise ValueError('browser request requires a supported mode and data array')
    ids, titles = set(), set()
    for row in rows:
        if (not isinstance(row, dict)
                or any(not isinstance(row.get(k), str) for k in ('id', 'title', 'project', 'status', 'description'))
                or not row['id'] or not row['title'].strip()
                or row['status'] not in ('running', 'completed', 'failed')):
            raise ValueError('browser request contains an invalid activity record')
        accessible_title = ' '.join(row['title'].replace('\u200b', '').replace('\u00ad', '')
                                    .replace('\ufeff', ' ').split())
        if accessible_title != row['title']:
            raise ValueError('browser request title must use canonical accessible-name whitespace')
        if row['id'] in ids or accessible_title.lower() in titles:
            raise ValueError('browser request requires unique ids and case-insensitive titles')
        ids.add(row['id']); titles.add(accessible_title.lower())
    if mode in ('detail', 'keyboard') and not rows:
        raise ValueError('browser request detail/keyboard requires a first record')
    if 'query' in request and not isinstance(request['query'], str):
        raise ValueError('browser request query must be a string')
    if 'status' in request and request['status'] not in ('Running', 'Completed', 'Failed'):
        raise ValueError('browser request status must select a supported label; omit for All')
    if mode in ('detail', 'keyboard', 'layout') and any(k in request for k in ('query', 'status')):
        raise ValueError('browser request cannot filter records used by detail/keyboard/layout probes')
    if 'largeText' in request and (type(request['largeText']) is not bool or mode != 'layout'):
        raise ValueError('browser request largeText must be boolean and requires layout mode')
    if 'viewport' in request:
        viewport = request['viewport']
        if (not isinstance(viewport, dict) or set(viewport) != {'width', 'height'}
                or any(type(v) is not int or not 1 <= v <= 4096 for v in viewport.values())):
            raise ValueError('browser request viewport needs integer width/height from 1 through 4096')


def _validate(files):
    if not REQUIRED <= files.keys():
        raise ValueError('package requires descriptor, cases, evaluator and solver instruction')
    for name in files:
        frozen_context._name(name)
        if name not in REQUIRED and not name.startswith(('solver/workspace/', 'fixtures/', 'controls/')):
            raise ValueError('undeclared package path')
    if not any(n.startswith('solver/workspace/web/') for n in files):
        raise ValueError('browser package requires a solver web checkout')
    descriptor = _json(files['evaluation.json'])
    if (not isinstance(descriptor, dict) or set(descriptor) !=
            {'schema', 'id', 'case_revision', 'oracle_revision', 'split', 'backend'}
            or type(descriptor['schema']) is not int or descriptor['schema'] != 1):
        raise ValueError('invalid evaluation descriptor')
    if not isinstance(descriptor['id'], str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}', descriptor['id']):
        raise ValueError('invalid evaluation id')
    if any(type(descriptor[k]) is not int or descriptor[k] < 1 for k in ('case_revision', 'oracle_revision')):
        raise ValueError('positive case and oracle revisions required')
    if descriptor['split'] not in ('development', 'holdout'):
        raise ValueError('split must be development or holdout')
    if not isinstance(descriptor['backend'], str) or descriptor['backend'] not in BACKENDS:
        raise ValueError('unknown public worker backend')
    cases = _json(files['cases.json'])
    if not isinstance(cases, list) or not 1 <= len(cases) <= 128:
        raise ValueError('package requires 1 through 128 cases')
    ids = set()
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {'id', 'bucket', 'request'}
                or not isinstance(case['request'], dict)
                or any(not isinstance(case[k], str) or not re.fullmatch('[a-zA-Z0-9_-]{1,80}', case[k])
                       for k in ('id', 'bucket')) or case['id'] in ids):
            raise ValueError('invalid or duplicate evaluation case')
        ids.add(case['id'])
        _browser_request(case['request'])
    # Validate syntax without executing imports or top-level code.
    compile(files['evaluator.py'], '<approved-evaluator>', 'exec')
    files['solver/instruction.md'].decode('utf-8')
    return descriptor, cases


@dataclass(frozen=True)
class Package:
    archive: frozen_context.Archive

    @property
    def descriptor(self):
        return _json(self.archive.files['evaluation.json'])

    @property
    def cases(self):
        return _json(self.archive.files['cases.json'])

    def inspect(self):
        return {'schema': 1, 'kind': 'evaluation-package', 'status': 'inspected',
                'sha256': self.archive.sha256, 'evaluation': self.descriptor,
                'solver_files': sorted(n.removeprefix('solver/') for n in self.archive.files if n.startswith('solver/')),
                'case_count': len(self.cases), 'buckets': sorted({c['bucket'] for c in self.cases}),
                'evaluator_executed': False, 'campaign_eligible': False}


def load(path, sha256):
    archive = frozen_context.load_archive(path, sha256, kind='evaluation')
    _validate(archive.files)
    return Package(archive)


def freeze(root, output):
    root = Path(root).absolute()
    if not root.is_dir() or root.is_symlink():
        raise ValueError('package root must be a real directory')
    files, modes = {}, {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('package cannot contain symlinks')
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        files[name], modes[name] = frozen_context._regular(path)
        if len(files) > frozen_context.MAX_FILES or sum(map(len, files.values())) > frozen_context.MAX_BYTES // 2:
            raise ValueError('package exceeds bounds')
    _validate(files)
    return frozen_context._write(files, modes, output, 'evaluation', {})


def export(package, destination):
    files = {n.removeprefix('solver/'): data for n, data in package.archive.files.items() if n.startswith('solver/')}
    manifest = {'files': {n: package.archive.manifest['files']['solver/' + n] for n in files}}
    # No task config, evaluator selection, hidden fixture, or control is exported.
    frozen_context.materialize(frozen_context.Archive(package.archive.sha256, manifest, files), destination)
    return {**package.inspect(), 'status': 'exported', 'destination': str(destination)}


def _checks(package, observations, *, trust_evaluator):
    if trust_evaluator is not True:
        raise ValueError('replay requires --trust-evaluator for operator-authored host code')
    namespace = {'__name__': '_approved_evaluation', '__file__': '<approved-evaluator>'}
    # Do not let ordinary print/log output corrupt CLI JSON or leak fixture text.
    # This is not a Python sandbox: the operator explicitly trusts this code.
    with tempfile.TemporaryFile(mode='w+') as log, redirect_stdout(log), redirect_stderr(log):
        try:
            exec(compile(package.archive.files['evaluator.py'], '<approved-evaluator>', 'exec'), namespace)
            grade = namespace.get('grade')
            if not callable(grade):
                raise ValueError('evaluator must define grade(cases, observations, fixtures)')
            verdicts = grade(package.cases, _json(json.dumps(observations, allow_nan=False)),
                             {n.removeprefix('fixtures/'): b for n, b in package.archive.files.items() if n.startswith('fixtures/')})
        except SystemExit as exc:
            raise ValueError('evaluator exited without completing its verdicts') from exc
    cases = package.cases  # A fresh copy: evaluator mutations cannot rewrite check identity.
    if not isinstance(verdicts, list) or len(verdicts) != len(cases):
        raise ValueError('evaluator must return one verdict per case')
    checks = []
    for case, verdict, observation in zip(cases, verdicts, observations, strict=True):
        if (not isinstance(verdict, dict) or set(verdict) != {'pass', 'reasons'}
                or type(verdict['pass']) is not bool or not isinstance(verdict['reasons'], list)
                or len(verdict['reasons']) > 16
                or any(not isinstance(reason, str) or not 1 <= len(reason) <= 200 for reason in verdict['reasons'])
                or verdict['pass'] != (not verdict['reasons'])):
            raise ValueError('invalid evaluator verdict: require boolean pass and bounded failure reasons')
        # An evaluator cannot turn worker failure into successful candidate behavior.
        failed = verdict['reasons'] if observation.get('ok') is True else ['worker-operation-failed']
        checks.append({'id': case['id'], 'bucket': case['bucket'], 'pass': not failed,
                       'failure_reasons': failed})
    return checks


def implementation():
    from .repair_oracles.registry import implementation_hashes
    hashes = implementation_hashes()
    for name in ('evaluation_package', 'frozen_context', 'repair_evidence'):
        hashes['obench.' + name] = hashlib.sha256(Path(__file__).with_name(name + '.py').read_bytes()).hexdigest()
    return hashes


def replay(path, sha256, source, image, *, trust_evaluator=False, timeout=90):
    if trust_evaluator is not True:
        raise ValueError('replay requires --trust-evaluator for operator-authored host code')
    validate_image(image)
    package = load(path, sha256)
    before = implementation()
    if before != _LOADED_IMPLEMENTATION:
        raise ValueError('public evaluation implementation changed after import')
    backend = BACKENDS[package.descriptor['backend']]
    runtime = backend.validate_runtime(image)
    files = read_tree(Path(source))
    if any(not n.startswith('web/') for n in files):
        raise ValueError('browser replay source must contain only the web subtree')
    # Capture once. Hash exactly the bytes sent to the confined worker.
    source_hashes = {n: hashlib.sha256(b).hexdigest() for n, b in sorted(files.items())}
    candidate_failure = None
    worker = None
    try:
        observations, worker = repair_worker.run_worker(image, pack_files(files),
            [c['request'] for c in package.cases], program=backend.worker_program(), timeout=timeout, browser=True)
    except CandidateFailure as exc:
        candidate_failure = exc.reason
        observations = [{'ok': False} for _ in package.cases]
    checks = _checks(package, observations, trust_evaluator=trust_evaluator)
    buckets = {c['bucket']: all(v['pass'] for v in checks if v['bucket'] == c['bucket']) for c in checks}
    score = sum(buckets.values()) / len(buckets)
    # Fail on changed archive or public implementation, even though this invocation
    # used captured bytes. Do not emit a successful report for a stale operator pin.
    load(path, sha256)
    if implementation() != before:
        raise ValueError('public evaluation implementation changed during replay')
    return {'schema': 1, 'kind': 'evaluation-package-replay', 'scope': SCOPE,
            'campaign_eligible': False, 'package_sha256': sha256, 'evaluation': package.descriptor,
            'implementation_sha256': before, 'runtime_image': image, 'runtime_dependencies': runtime,
            'source_sha256': source_hashes, 'candidate_failure': candidate_failure,
            'worker': worker, 'observations': observations, 'checks': checks, 'buckets': buckets,
            'score': score, 'solved': all(c['pass'] for c in checks)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='obench repair package', description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('freeze', 'inspect', 'export', 'replay'):
        p = sub.add_parser(action)
        p.add_argument('input', type=Path, help='authoring directory' if action == 'freeze' else 'frozen archive')
        p.add_argument('--json', action='store_true')
        if action == 'freeze':
            p.add_argument('--output', type=Path, required=True)
        else:
            p.add_argument('--sha256', required=True, help='operator-approved archive SHA256')
        if action == 'export':
            p.add_argument('--destination', type=Path, required=True, help='fresh solver projection directory')
        if action == 'replay':
            p.add_argument('--source', type=Path, required=True, help='frozen submission web subtree root')
            p.add_argument('--image', required=True, help='immutable browser runtime image')
            p.add_argument('--trust-evaluator', action='store_true', help='authorize this pinned evaluator as host code')
            p.add_argument('--output', type=Path, required=True, help='fresh private diagnostic report')
    args = parser.parse_args(argv)
    try:
        if args.action == 'freeze':
            sha = freeze(args.input, args.output)
            result = {**load(args.output, sha).inspect(), 'status': 'frozen'}
        elif args.action == 'inspect':
            result = load(args.input, args.sha256).inspect()
        elif args.action == 'export':
            result = export(load(args.input, args.sha256), args.destination)
        else:
            if args.output.exists() or args.output.is_symlink():
                raise ValueError('output exists; choose a fresh evidence path')
            with Operation(str(args.output) + '.evidence', 'package-replay') as evidence:
                with evidence.stage('replay'):
                    result = replay(args.input, args.sha256, args.source, args.image,
                                    trust_evaluator=args.trust_evaluator)
                write_json(args.output, result, exclusive=True)
                evidence.finish(result)
        print(json.dumps(result, sort_keys=True) if args.json else
              (f"Replay completed: score={result['score']}; {SCOPE}" if args.action == 'replay' else
               f"Package {result['status']}: {result['sha256']}; campaign eligible=False"))
        return 1 if result.get('solved') is False else 0
    except Exception as exc:
        # Trusted plugin failures are incomplete evaluation, never a candidate miss.
        error = {'schema': 1, 'status': 'incomplete', 'error_type': type(exc).__name__, 'error': str(exc)[:2000]}
        if args.json:
            print(json.dumps(error))
        else:
            print('package operation incomplete: ' + error['error'], file=sys.stderr)
        return 2


_LOADED_IMPLEMENTATION = implementation()


if __name__ == '__main__':
    raise SystemExit(main())
