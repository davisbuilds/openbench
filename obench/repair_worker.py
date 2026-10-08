"""Shared confined worker lifecycle for versioned trusted repair oracles.

The legacy module stays frozen for scheme-3 replay. Its source extraction and
bounded process helpers are reused and explicitly bound by scheme 4.
"""
import base64
import json
import math
import re
import subprocess
import uuid

from .sandbox_grading import (CandidateFailure, GradingError, _CommandFailure,
                              bounded_command, source_archive, valid_worker_result)


def strict_json(raw):
    def pairs(items):
        value={}
        for key, item in items:
            if key in value:
                raise ValueError('duplicate worker output key')
            value[key]=item
        return value
    def number(text):
        value=float(text)
        if not math.isfinite(value):
            raise ValueError('non-finite worker output')
        return value
    def constant(_):
        raise ValueError('non-finite worker output')
    return json.loads(raw, object_pairs_hook=pairs, parse_float=number, parse_constant=constant)


def run_worker(image: str, archive: bytes, cases: list[dict], *, program: str, timeout=30, browser=False) -> tuple[list, dict]:
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
              '--pids-limit','64','--memory','512m','--cpus','1',
              '--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777',
              '--workdir','/app','--interactive','--entrypoint','python3',image,'-I','-c',program]
    if browser:
        from .browser_policy import POLICY
        create[create.index('--pids-limit') + 1] = '256'
        create[create.index('--memory') + 1] = '2048m'
        create[create.index('--tmpfs') + 1] = '/tmp:rw,nosuid,nodev,size=536870912,mode=1777'
        create[2:2] = ['--security-opt', 'seccomp=' + str(POLICY), '--shm-size', '256m']
    created = False
    try:
        bounded_command(create)
        created = True
        inspected = json.loads(bounded_command(['docker','inspect',name]))[0]
        host = inspected['HostConfig']
        if browser:
            from .browser_policy import verify_options
            if not verify_options(host.get('SecurityOpt')) or host.get('IpcMode') != 'private':
                raise GradingError('browser worker sandbox policy drift')
        if (inspected['Image'] != image or inspected['Config']['User'] != '10001:10001'
                or host['NetworkMode'] != 'none' or not host['ReadonlyRootfs']
                or host.get('Binds') or inspected.get('Mounts')
                or 'ALL' not in host.get('CapDrop', [])
                or not any(s.startswith('no-new-privileges') for s in host.get('SecurityOpt', []))):
            raise GradingError('worker configuration drift')
        try:
            raw = bounded_command(['docker','start','--attach','--interactive',name],
                                  input_bytes=json.dumps({'source':base64.b64encode(archive).decode(),'cases':cases}).encode(), timeout=timeout,
                                  **({'limit':8*1024*1024} if browser else {}))
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
            result = strict_json(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise CandidateFailure('malformed worker output') from exc
        if (not isinstance(result, dict) or set(result) != {'schema','results'}
                or type(result['schema']) is not int or result['schema'] != 1 or not isinstance(result['results'], list)
                or len(result['results']) != len(cases)):
            raise CandidateFailure('invalid worker protocol')
        for value in result['results']:
            if not valid_worker_result(value):
                raise CandidateFailure('invalid worker result')
        evidence = {'image_id':image,'requested_image':requested_image,'network':'none','user':'10001:10001',
                    'host_mounts':False,'read_only_root':True,'capabilities':'none'}
        if browser:
            from .browser_policy import identity
            evidence['browser_policy_sha256'] = identity()
        return result['results'], evidence
    finally:
        if created:
            cleaned = subprocess.run(['docker','rm','--force',name], capture_output=True, timeout=30)
            if cleaned.returncode:
                raise GradingError('worker cleanup failed')


def runtime_probe(image, argv):
    """Check installed runtime dependencies before any candidate code is loaded."""
    if not re.fullmatch(r'(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[0-9a-f]{64}',image):
        raise GradingError('runtime probe requires an immutable image')
    name='obench-grade-probe-'+uuid.uuid4().hex
    created=False
    try:
        bounded_command(['docker','create','--name',name,'--network','none','--user','10001:10001',
            '--cap-drop','ALL','--security-opt','no-new-privileges:true','--read-only',
            '--pids-limit','64','--memory','256m','--cpus','1',image,*argv])
        created=True
        return strict_json(bounded_command(['docker','start','--attach',name],timeout=30))
    except (ValueError,_CommandFailure) as exc:
        raise GradingError('registered worker runtime dependency probe failed') from exc
    finally:
        if created:
            result=subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=30)
            if result.returncode: raise GradingError('runtime dependency probe cleanup failed')
