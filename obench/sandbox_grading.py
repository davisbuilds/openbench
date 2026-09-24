"""Trusted repair grading. Candidate Python runs only in a disposable Docker worker.

The public worker receives inputs, never expected answers, oracle source, reward
paths, credentials or host mounts. Harbor is an optional, lazy dependency.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tarfile
import tempfile
import time
import tomllib
import uuid

# Bind the implementation loaded by this process, not a later edit on disk.
_LOADED_GRADER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

MAX_SOURCE = 2 * 1024 * 1024
MAX_OUTPUT = 128 * 1024

# Public protocol implementation; safe for candidates to inspect. No oracle.
WORKER = r'''
import base64, io, json, sys, tarfile, tempfile
from pathlib import Path
request = json.loads(sys.stdin.buffer.read(4194305))
source = tempfile.TemporaryDirectory(prefix='candidate-')
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(request['source']))) as tar:
    tar.extractall(source.name, filter='data')
sys.path.insert(0, source.name)
from scripts.profiles import budget, rollout_codex
from scripts.profiles.probe_codex import parse_block

def read(records):
    meta = {'type':'session_meta','payload':{'originator':'codex-tui','cli_version':'fixture','cwd':'/synthetic/work','model':'fixture'}}
    with tempfile.TemporaryDirectory() as directory:
        p = Path(directory)/'rollout.jsonl'
        p.write_text('\n'.join(json.dumps(r) for r in [meta,*records])+'\n')
        return rollout_codex.read_rollout(p)

def invoke(case):
    op = case['op']
    if op == 'read':
        observed = read(case['records'])
        return None if observed is None else {'names':[e.name for e in observed.listing.entries], 'surface':observed.meta.surface}
    if op == 'budget':
        p = budget.Policy(**case['policy'])
        if case.get('direct'):
            result = budget.Assessment(p,3999,4000,'tokens',budget.Verdict.NONCONFORMANT,surface=case['surface'])
            return {'gating':result.gating}
        result = budget.assess(case['entries'],p,surface=case['surface'])
        return {'verdict':result.verdict.value,'gating':result.gating,'demand':result.demand,'entries_scored':result.entries_scored}
    if op == 'mismatch':
        return rollout_codex.surface_mismatch(parse_block(case['live']),read(case['recorded']))
    raise ValueError('unknown operation')

result = []
for case in request['cases']:
    try:
        result.append({'ok':True,'value':invoke(case)})
    except Exception:
        result.append({'ok':False})
sys.stdout.write(json.dumps({'schema':1,'results':result},allow_nan=False))
'''


class GradingError(ValueError):
    """The execution/evidence boundary failed; not a model verdict."""


class CandidateFailure(GradingError):
    """Invalid candidate behavior/submission: trusted zero, never infra exclusion."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _CommandFailure(GradingError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def task_manifest(task_root: Path) -> dict:
    """Scheme 3 binds the actual Harbor task tree and external trusted grader.

    task.toml is canonicalized as parsed TOML; only the self-referential digest
    metadata is removed. Every other regular task file, including dotfiles, is
    bound. Symlinks and special files are rejected instead of silently omitted.
    This is distinct from native-task publication digest schemes 1 and 2.
    """
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != _LOADED_GRADER_SHA256:
        raise GradingError('trusted grader changed after import')
    root = Path(task_root)
    if root.is_symlink() or not root.is_dir():
        raise GradingError('invalid task root')
    task_file = root / 'task.toml'
    if task_file.is_symlink() or not task_file.is_file():
        raise GradingError('invalid task configuration')
    config = tomllib.loads(task_file.read_text())
    metadata = config.get('metadata', {})
    if not isinstance(metadata, dict):
        raise GradingError('invalid task metadata')
    metadata.pop('openbench_task_content_digest', None)
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise GradingError('unsupported task artifact type')
            if stat.S_ISREG(mode) and path != task_file:
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        'scheme': 3,
        'schema': 'openbench-isolated-repair-task-v1',
        'task_config': config,
        'task_files_sha256': dict(sorted(files.items())),
        'grading_module_sha256': _LOADED_GRADER_SHA256,
        'worker_entry_sha256': hashlib.sha256(WORKER.encode()).hexdigest(),
    }


def task_digest(task_root: Path) -> str:
    manifest = task_manifest(task_root)
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def validate_task_binding(task_root: Path, expected: dict) -> dict:
    if (not isinstance(expected, dict) or set(expected) != {'scheme', 'sha256'}
            or type(expected.get('scheme')) is not int or expected['scheme'] != 3
            or not isinstance(expected.get('sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])):
        raise GradingError('isolated grading requires a scheme 3 task binding')
    manifest = task_manifest(task_root)
    actual = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'),
                                       allow_nan=False).encode()).hexdigest()
    disk_binding = tomllib.loads((Path(task_root) / 'task.toml').read_text()).get('metadata', {}).get('openbench_task_content_digest')
    if disk_binding != expected or actual != expected['sha256']:
        raise GradingError('trusted task or grader binding mismatch')
    return manifest


def source_archive(root: Path, permitted: set[str]) -> tuple[bytes, dict]:
    """Validate a frozen directory without following candidate links or devices."""
    root = Path(root)
    if not permitted or any(Path(name).is_absolute() or '..' in Path(name).parts
                            or Path(name).as_posix() != name for name in permitted):
        raise GradingError('invalid source allowlist')
    if root.is_symlink() or not root.is_dir():
        raise CandidateFailure('invalid source root')
    files = {}
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            p = Path(directory) / name
            mode = p.lstat().st_mode
            if stat.S_ISDIR(mode):
                if not any(s.startswith(p.relative_to(root).as_posix() + '/') for s in permitted):
                    raise CandidateFailure('unexpected artifact directory')
                continue
            relative = p.relative_to(root).as_posix()
            if not stat.S_ISREG(mode) or p.stat().st_nlink != 1 or relative not in permitted:
                raise CandidateFailure('unexpected artifact path or type')
            # Solver has stopped; O_NOFOLLOW also defends against accidental replacement.
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                data = stream.read(MAX_SOURCE + 1)
            total += len(data)
            if total > MAX_SOURCE:
                raise CandidateFailure('source size limit')
            files[relative] = data
    if set(files) != permitted:
        raise CandidateFailure('missing source files')
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o444
            tar.addfile(info, io.BytesIO(data))
    receipt = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    return archive.getvalue(), receipt


def bounded_command(argv, *, input_bytes=b'', timeout=30, limit=MAX_OUTPUT):
    """Bound combined stdout/stderr while draining both; never buffer unbounded output."""
    with tempfile.TemporaryFile() as source:
        source.write(input_bytes)
        source.seek(0)
        process = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chunks = {process.stdout: bytearray(), process.stderr: bytearray()}
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        try:
            for stream in chunks:
                selector.register(stream, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _CommandFailure('worker timeout')
                for key, _ in selector.select(min(remaining, .1)):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                    else:
                        chunks[key.fileobj].extend(data)
                        if sum(map(len, chunks.values())) > limit:
                            raise _CommandFailure('worker output limit')
            process.wait(timeout=max(.01, deadline - time.monotonic()))
            if process.returncode:
                raise _CommandFailure('worker command failed')
            return bytes(chunks[process.stdout])
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in chunks:
                stream.close()


def run_worker(image: str, archive: bytes, cases: list[dict], *, timeout=30) -> tuple[list, dict]:
    requested_image = image
    if not re.fullmatch(r'(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[0-9a-f]{64}', image):
        raise GradingError('worker image must be pinned by digest or image ID')
    resolved = json.loads(bounded_command(['docker','image','inspect',image]))
    if not isinstance(resolved,list) or len(resolved) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', resolved[0].get('Id','')):
        raise GradingError('cannot resolve immutable worker image')
    image = resolved[0]['Id']
    name = 'obench-grade-' + uuid.uuid4().hex
    create = ['docker','create','--name',name,'--network','none','--user','10001:10001',
              '--cap-drop','ALL','--security-opt','no-new-privileges:true','--read-only',
              '--pids-limit','64','--memory','256m','--cpus','1',
              '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=16777216,mode=1777',
              '--workdir','/app','--interactive','--entrypoint','python3',image,'-I','-c',WORKER]
    created = False
    try:
        bounded_command(create)
        created = True
        inspected = json.loads(bounded_command(['docker','inspect',name]))[0]
        host = inspected['HostConfig']
        if (inspected['Image'] != image or inspected['Config']['User'] != '10001:10001'
                or host['NetworkMode'] != 'none' or not host['ReadonlyRootfs']
                or host.get('Binds') or inspected.get('Mounts')
                or 'ALL' not in host.get('CapDrop', [])
                or not any(s.startswith('no-new-privileges') for s in host.get('SecurityOpt', []))):
            raise GradingError('worker configuration drift')
        try:
            raw = bounded_command(['docker','start','--attach','--interactive',name],
                                  input_bytes=json.dumps({'source':base64.b64encode(archive).decode(),'cases':cases}).encode(), timeout=timeout)
        except _CommandFailure as exc:
            # A daemon/start failure is infrastructure. Only classify failures as
            # candidate-caused after Docker proves the worker actually started.
            state = json.loads(bounded_command(['docker','inspect',name]))[0]['State']
            started = (isinstance(state.get('StartedAt'),str) and bool(state['StartedAt'])
                       and not state['StartedAt'].startswith('0001-'))
            process_failed = (state.get('Running') is False
                              and type(state.get('ExitCode')) is int and state['ExitCode'] != 0)
            candidate_cause = (exc.reason == 'worker output limit'
                               or (exc.reason == 'worker timeout' and state.get('Running') is True)
                               or process_failed)
            if not state.get('Error') and started and candidate_cause:
                raise CandidateFailure(exc.reason) from None
            raise
        try:
            result = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('constant')))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise CandidateFailure('malformed worker output') from exc
        if (not isinstance(result, dict) or set(result) != {'schema','results'}
                or result['schema'] != 1 or not isinstance(result['results'], list)
                or len(result['results']) != len(cases)):
            raise CandidateFailure('invalid worker protocol')
        for value in result['results']:
            if not isinstance(value,dict) or type(value.get('ok')) is not bool or set(value) != ({'ok','value'} if value['ok'] else {'ok'}):
                raise CandidateFailure('invalid worker result')
        return result['results'], {'image_id':image,'requested_image':requested_image,'network':'none','user':'10001:10001',
                                  'host_mounts':False,'read_only_root':True,'capabilities':'none'}
    finally:
        if created:
            cleaned = subprocess.run(['docker','rm','--force',name], capture_output=True, timeout=30)
            if cleaned.returncode:
                raise GradingError('worker cleanup failed')


def block(entries, roots=()):
    intro = 'a short path that can be expanded into an absolute path using the skill roots table' if roots else 'Each entry includes a name, description, and source locator.'
    return '\n'.join(['<skills_instructions>','## Skills',intro,*(['### Skill roots',*roots] if roots else []),
                      '### Available skills',*[f'- {name}: Synthetic description. (file: {locator})' for name,locator in entries],'</skills_instructions>'])


def record(role, text):
    return {'type':'response_item','payload':{'type':'message','role':role,'content':[{'type':'input_text','text':text}]}}


def dojo_cases(*, oracle_version=3):
    """Hidden oracle inputs and expected comparisons remain in the trusted process."""
    if type(oracle_version) is not int or oracle_version not in (3, 4):
        raise GradingError('unsupported Dojo oracle version')
    dojo='/synthetic/.agents/skills/review/SKILL.md'
    bundled='/synthetic/.codex/skills/.system/review/SKILL.md'
    connector='/synthetic/.codex/plugins/cache/openai-curated-remote/demo/1/review/SKILL.md'
    cases=[]
    def add(bucket, request, expected): cases.append((bucket,request,expected))
    actual=record('developer',block([('actual',dojo)]))
    add('rollout',{'op':'read','records':[actual]}, {'names':['actual'],'surface':'codex-tui'})
    decoys=[record(role,block([('quoted',connector)])) for role in ('user','assistant','tool')]
    decoys.append({'type':'compacted','payload':{'message':block([('quoted',connector)])}})
    for decoy in decoys: add('rollout',{'op':'read','records':[decoy,actual]}, {'names':['actual'],'surface':'codex-tui'})
    add('rollout',{'op':'read','records':decoys},None)
    add('rollout',{'op':'read','records':[record('developer',block([]))]}, {'names':[],'surface':'codex-tui'})
    add('rollout',{'op':'read','records':[record('developer','No skills listing.')]},None)
    policy=dict(harness='codex',harness_version='fixture',model='fixture',unit='tokens',limit=4000,context_window=None,window_field=None,estimator='fixture',provenance='synthetic',measured='fixture',probe='fixture',deployable=True,shadows_by_name=False,project_scope_root='.agents/skills',limit_basis='observed',declared_surfaces=['codex-tui'])
    entries=[dict(name='review',source_description='Short description.',listed_description='Short description.',locator=dojo)]
    for surface in ['codex-tui','exec','codex_exec',None]:
        add('budget',dict(op='budget',policy=policy,entries=entries,surface=surface), 'budget-positive' if surface=='codex-tui' else 'budget-negative')
    for surface in [None,'exec','codex-tui']:
        add('budget',dict(op='budget',policy=policy,surface=surface,direct=True),dict(gating=surface=='codex-tui'))
    def mismatch(live, recorded, expected, roots=()):
        add('mismatch',dict(op='mismatch',live=block(live),recorded=[record('developer',block(recorded,roots))]),expected)
    mismatch([('review',dojo),('review',connector)],[('review','r0/demo/2/review/SKILL.md'),('review',dojo)],None,['- `r0` = `/synthetic/.codex/plugins/cache/openai-curated-remote`'])
    mismatch([('review',dojo)],[('review',dojo),('review',bundled)],(1,2,0,1))
    mismatch([('review',dojo)],[('review',dojo)]*3,(1,3,0,2))
    mismatch([('review',dojo)],[('review',connector)],(1,1,1,1))
    if oracle_version == 4:
        # Paired equality/difference cases prevent an always-mismatch repair.
        # These expectations name observable outcomes, not a reference algorithm.
        for entries in ([], [('review', dojo)], [('review', dojo)] * 3,
                        [('review', dojo), ('review', bundled), ('other', connector)]):
            mismatch(entries, list(reversed(entries)), None)
        mismatch([('review', dojo)] * 3, [('review', dojo)], (3, 1, 2, 0))
        mismatch([('review', connector)], [('review', dojo)], (1, 1, 1, 1))
        mismatch([('review', bundled)], [('review', connector)], (1, 1, 1, 1))
        mismatch([], [('review', dojo)], (0, 1, 0, 1))
        mismatch([('review', dojo)], [], (1, 0, 1, 0))
        mismatch([('other', dojo)], [('review', dojo)], (1, 1, 1, 1))
        mismatch([('review', dojo)], [('review', 'r0/review/SKILL.md')], None,
                 ['- `r0` = `/synthetic/.agents/skills`'])
        mismatch([('review', connector)],
                 [('review', connector.replace('/demo/1/', '/demo/2/'))], None)
    return cases


def comparison(value, expected, *, oracle_version=3):
    if expected == 'budget-positive':
        return (isinstance(value,dict) and value.get('verdict')=='deployable' and value.get('gating') is True
                and type(value.get('demand')) in (int,float) and value['demand']>0 and value.get('entries_scored')==1)
    if expected == 'budget-negative':
        return (isinstance(value,dict) and value.get('verdict')=='unsupported' and value.get('gating') is False and value.get('entries_scored')==0)
    if isinstance(expected, tuple) and oracle_version == 4:
        # Preserve the public diagnostic shape, but do not prescribe whether
        # difference arrays contain names, qualified identities or duplicates.
        return (isinstance(value, dict) and value.get('kind') == 'surface-mismatch'
                and all(type(value.get(key)) is int for key in ('live_entries', 'recorded_entries'))
                and (value['live_entries'], value['recorded_entries']) == expected[:2]
                and all(isinstance(value.get(key), list)
                        and all(isinstance(item, str) for item in value[key])
                        for key in ('only_in_live', 'only_in_recorded'))
                and all(isinstance(value.get(key), str)
                        for key in ('live_surface', 'recorded_surface', 'detail')))
    if isinstance(expected,tuple):
        return (isinstance(value,dict) and value.get('kind')=='surface-mismatch'
                and (value.get('live_entries'),value.get('recorded_entries'))==expected[:2]
                and isinstance(value.get('only_in_live'),list) and isinstance(value.get('only_in_recorded'),list)
                and (len(value['only_in_live']),len(value['only_in_recorded']))==expected[2:])
    return value == expected


def grade_dojo(root, permitted, image, *, timeout=30, oracle_version=3):
    archive, hashes = source_archive(Path(root),set(permitted))
    cases=dojo_cases(oracle_version=oracle_version)
    results, receipt=run_worker(image,archive,[case[1] for case in cases],timeout=timeout)
    buckets={name:True for name in ('rollout','budget','mismatch')}
    checks=[]
    for (bucket,_,expected), result in zip(cases,results):
        passed=result['ok'] and comparison(result.get('value'),expected,oracle_version=oracle_version)
        buckets[bucket] &= passed
        checks.append({'bucket':bucket,'pass':passed})
    return {'score':round(sum(buckets.values())/3,4),'buckets':buckets,'checks':checks,
            'source_sha256':hashes,'worker':receipt,'oracle_version':oracle_version}


def candidate_failure_result(reason):
    return {'score': 0.0, 'candidate_failure': reason,
            'buckets': {name: False for name in ('rollout', 'budget', 'mismatch')},
            'checks': [], 'source_sha256': None, 'worker': None}


def grade_submission(root, permitted, image, *, timeout=30, oracle_version=3):
    try:
        return grade_dojo(root, permitted, image, timeout=timeout, oracle_version=oracle_version)
    except CandidateFailure as exc:
        return candidate_failure_result(exc.reason)


async def freeze_submission(environment, destination):
    # Importing the exception does not import optional Harbor dependencies.
    from .harbor_sandbox import SandboxArtifactError
    failure = None
    try:
        receipt = await environment.freeze_source(destination)
    except SandboxArtifactError as exc:
        receipt = exc.receipt
        failure = candidate_failure_result('invalid_source_artifact')
    if receipt.get('solver_stopped') is not True or receipt.get('broker_revoked') is not True:
        raise GradingError('unsealed solver')
    return receipt, failure


def dojo_oracle_version(metadata):
    versions = {'dojo-evidence-pr60-v3': 3, 'dojo-evidence-pr60-v4': 4}
    name = metadata.get('openbench_task')
    if not isinstance(name, str) or name not in versions:
        raise GradingError('unsupported Dojo task identity')
    return versions[name]


class _TrustedDojoVerifier:
    def __init__(self,*args,worker_image=None,worker_timeout=30,**kwargs):
        super().__init__(*args,**kwargs)
        if not worker_image:
            raise GradingError('worker_image is required')
        self.worker_image=worker_image
        self.worker_timeout=worker_timeout

    async def verify(self):
        from harbor.models.verifier.result import VerifierResult
        start=time.monotonic()
        metadata=self.task.config.metadata
        oracle_version=dojo_oracle_version(metadata)
        binding=validate_task_binding(self.task.paths.task_dir, metadata.get('openbench_task_content_digest'))
        app=self.task.paths.environment_dir/'app'
        permitted={p.relative_to(app).as_posix() for p in (app/'scripts/profiles').glob('*.py')}
        if not permitted:
            raise GradingError('missing trusted source allowlist')
        with tempfile.TemporaryDirectory(prefix='obench-frozen-') as directory:
            freeze, graded=await freeze_submission(self.environment,Path(directory))
            # Freeze may await cleanup; detect trusted-task drift again before work.
            validate_task_binding(self.task.paths.task_dir, metadata['openbench_task_content_digest'])
            if graded is None:
                graded=await asyncio.to_thread(grade_submission,Path(directory),permitted,self.worker_image,timeout=self.worker_timeout,oracle_version=oracle_version)
        validate_task_binding(self.task.paths.task_dir, metadata['openbench_task_content_digest'])
        logs=self.trial_paths.verifier_dir
        logs.mkdir(parents=True,exist_ok=True)
        score=graded['score']
        evidence={'schema_version':'openbench-verifier-evidence-v2',
                  'openbench_task_content_digest':metadata['openbench_task_content_digest'],
                  'openbench_harbor_export':metadata['openbench_harbor_export'],
                  'checker_exit':0 if score==1 else 1,'parsed_score':score,'reward':score,
                  'verifier_duration_seconds':time.monotonic()-start}
        # Only this trusted process has these paths; worker never receives them.
        (logs/'reward.txt').write_text(str(score)+'\n')
        (logs/'openbench-verifier-evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
        (logs/'sandbox-grading.json').write_text(json.dumps({'freeze':freeze,'grading':graded,'task_binding':binding},indent=2)+'\n')
        return VerifierResult(rewards={'reward':score})


def __getattr__(name):
    if name in ('TrustedDojoVerifier', 'RepairVerifier'):
        from harbor.verifier.base import BaseVerifier
        cls=type(name,(_TrustedDojoVerifier,BaseVerifier),{'__module__':__name__})
        globals()[name]=cls
        return cls
    raise AttributeError(name)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Compute or check an isolated repair task binding.')
    parser.add_argument('task_root', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    binding = {'scheme': 3, 'sha256': task_digest(args.task_root)}
    if args.check:
        configured = tomllib.loads((args.task_root / 'task.toml').read_text()).get('metadata', {}).get('openbench_task_content_digest')
        validate_task_binding(args.task_root, configured)
    print(json.dumps(binding, sort_keys=True))
