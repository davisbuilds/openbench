"""Persistent local supervision for canonical Harbor suites.

Harbor remains the executor. Launch receipts are local operator evidence, not
benchmark results. An interrupted supervisor never establishes trial completion.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import uuid

from .suite_run import compile_suite, plan_jobs, run_suite, verify_suite_run

SCHEMA = 1
ROOT = Path(__file__).resolve().parents[1]


class CampaignError(ValueError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_record(path, value):
    """Atomic, private receipts; a failed write is a failed supervisor."""
    temporary = path.with_suffix('.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_record(path):
    with path.open(encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise CampaignError(f'unsupported campaign receipt: {path}')
    return value


def git_identity(root=ROOT):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    if git('status', '--porcelain'):
        raise CampaignError('campaign execution requires a clean source checkout')
    commit = git('rev-parse', 'HEAD')
    if not git('branch', '-r', '--contains', commit):
        raise CampaignError('campaign execution requires an exact pushed source commit')
    return {'root': str(root), 'commit': commit}


def session_exists(session, directory=None):
    result = subprocess.run(['tmux', 'has-session', '-t', '=' + session],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        return False
    if directory is not None:
        marker = subprocess.run(['tmux','show-environment','-t','='+session,'OPENBENCH_CAMPAIGN_DIRECTORY'],
                                capture_output=True,text=True)
        return marker.returncode == 0 and marker.stdout.strip() == 'OPENBENCH_CAMPAIGN_DIRECTORY=' + str(directory)
    return True


@contextmanager
def execution_lock(directory, *, blocking=False):
    # Created before launch; status must never create a missing lock file.
    with (directory / 'execution.lock').open('r+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise CampaignError('campaign supervisor is active') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _environment():
    # tmux's server may have an old or credential-bearing ambient environment.
    # Pass only explicit local execution paths; never serialize API keys.
    allowed = ('HOME', 'PATH', 'TMPDIR', 'DOCKER_HOST', 'DOCKER_CONTEXT',
               'DOCKER_CONFIG', 'CODEX_HOME')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env['PYTHONPATH'] = str(ROOT)
    env['PYTHONNOUSERSITE'] = '1'
    return env


def spawn_supervisor(directory, launch, command, env):
    """Create tmux while holding the worker lock, closing the launch race."""
    with execution_lock(directory):
        if session_exists(launch['session']):
            raise CampaignError('campaign tmux session already exists')
        argv = ['tmux', 'new-session', '-d', '-s', launch['session'], '-c', launch['source']['root'],
                '-e', 'OPENBENCH_CAMPAIGN_DIRECTORY=' + str(directory),
                '/usr/bin/env', '-i', *[f'{key}={value}' for key, value in sorted(env.items())], *command]
        try:
            subprocess.run(argv, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            write_record(directory / 'finished.json', {'schema': SCHEMA, 'state': 'launch_failed',
                         'finished_at': stamp(), 'exit_code': 1})
            raise CampaignError('tmux could not start the supervisor; launch receipt retained') from exc


def launch_campaign(suite, *, harbor_binary='harbor', admission=None, qualify=False, auth_file=None):
    if not shutil.which('tmux'):
        raise CampaignError('tmux is required for persistent campaign execution')
    source = git_identity()
    compiled = compile_suite(suite)
    jobs = plan_jobs(compiled)
    from . import runtime_admission
    admission_hash = None
    if qualify and not auth_file:
        raise CampaignError('qualification requires an explicit local OAuth file')
    if compiled.suite.sandbox and not qualify:
        if not admission:
            raise CampaignError('repair campaigns require --admission from a passing qualification')
        expected = runtime_admission.fingerprint(compiled, harbor_binary)
        runtime_admission.validate_admission(admission, expected)
        admission_hash = runtime_admission.digest(admission)
    if qualify:
        qualification_fingerprint = runtime_admission.fingerprint(compiled, harbor_binary)
        qualification_key = hashlib.sha256(json.dumps(qualification_fingerprint,sort_keys=True).encode()).hexdigest()
    harbor = shutil.which(harbor_binary)
    if not harbor:
        raise CampaignError('Harbor executable not found')
    base = Path(compiled.config.results_dir) / 'campaigns'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = base / (('qualification-' + qualification_key[:24] + '-' + uuid.uuid4().hex[:12]) if qualify else compiled.manifest_sha256)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        raise CampaignError(f'campaign already has launch intent: {directory}; inspect status and Harbor state before recovery') from None
    (directory / 'execution.lock').touch(mode=0o600)
    launch = {'schema': SCHEMA, 'created_at': stamp(), 'host': socket.gethostname(),
              'source': source, 'suite': str(compiled.suite.path),
              'manifest_sha256': compiled.manifest_sha256, 'mode': 'qualify' if qualify else 'run',
              'admission': str(Path(admission).resolve()) if admission else None,
              'admission_sha256': admission_hash,
              'auth_file': str(Path(auth_file).expanduser().absolute()) if qualify else None,
              'manifest': compiled.manifest, 'harbor_binary': str(Path(harbor).absolute()),
              'python': sys.executable, 'session': ('obench-qualify-' + qualification_key[:24]) if qualify else ('obench-' + compiled.manifest_sha256[:24]),
              'jobs': [] if qualify else [str(Path(compiled.config.jobs_dir) / j.artifact.job_name) for j in jobs],
              'idle_sleep_prevention': sys.platform == 'darwin'}
    write_record(directory / 'launch.json', launch)
    command = [sys.executable, '-m', 'obench.campaign', '_execute', str(directory)]
    if sys.platform == 'darwin':
        command = ['/usr/bin/caffeinate', '-i', *command]
    spawn_supervisor(directory, launch, command, _environment())
    return directory


def execute(directory):
    directory = Path(directory).resolve()
    launch = read_record(directory / 'launch.json')
    with execution_lock(directory, blocking=True):
        if (directory / 'finished.json').exists():
            raise CampaignError('completed launch cannot execute again')
        # All subsequent stdout/stderr (including Harbor) goes to the private log.
        with (directory / 'campaign.log').open('a', buffering=1) as log:
            os.chmod(directory / 'campaign.log', 0o600)
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
            write_record(directory / 'worker.json', {'schema': SCHEMA, 'pid': os.getpid(), 'started_at': stamp()})
            code, state = 1, 'failed'
            record = {'schema': SCHEMA}
            try:
                if socket.gethostname() != launch['host'] or git_identity() != launch['source']:
                    raise CampaignError('execution host or source changed since launch')
                compiled = compile_suite(launch['suite'])
                if compiled.manifest_sha256 != launch['manifest_sha256']:
                    raise CampaignError('suite inputs changed since launch')
                from . import runtime_admission
                if launch.get('mode') == 'qualify':
                    admission = runtime_admission.qualify(compiled, directory, launch['harbor_binary'], launch['auth_file'])
                    record['admission'] = str(admission)
                    code, state = 0, 'qualified'
                else:
                    if compiled.suite.sandbox:
                        admission = launch['admission']
                        if runtime_admission.digest(admission) != launch['admission_sha256']:
                            raise CampaignError('admission changed after launch')
                        runtime_admission.validate_admission(admission, runtime_admission.fingerprint(compiled, launch['harbor_binary']))
                    result = run_suite(compiled, harbor_binary=launch['harbor_binary'], finalize=True)
                    code = result.returncode
                    if code == 0 and result.run_manifest_path is not None:
                        state = 'completed'
                        record['run_manifest'] = str(result.run_manifest_path)
                        record['result_count'] = result.result_count
                    else:
                        state = 'failed'
            except Exception as exc:
                # Exception text may contain provider/config details; keep it in
                # the local log, not in the metadata status surface.
                print(f'{type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
                record['error_type'] = type(exc).__name__
            finally:
                record.update(state=state, exit_code=code, finished_at=stamp())
                write_record(directory / 'finished.json', record)
            return code


def campaign_status(directory):
    directory = Path(directory).resolve()
    launch = read_record(directory / 'launch.json')
    if socket.gethostname() != launch['host']:
        raise CampaignError('inspect process status on the recorded execution host')
    try:
        with execution_lock(directory):
            active = False
    except CampaignError:
        active = True
    finished_path = directory / 'finished.json'
    finished = read_record(finished_path) if finished_path.exists() else None
    tmux_alive = session_exists(launch['session'], directory)
    state = finished['state'] if finished else 'running' if active else 'starting' if tmux_alive else 'interrupted_or_not_started'
    trials, outcomes, errors = [], Counter(), []
    if finished and finished['state'] == 'completed':
        try:
            verify_suite_run(finished['run_manifest'])
        except (OSError, ValueError, KeyError):
            state = 'completion_evidence_invalid'
            errors.append('sealed suite verification failed')
    if finished and finished['state'] == 'qualified':
        try:
            from .runtime_admission import validate_admission
            admission = Path(finished['admission'])
            # Status validates the recorded qualification evidence. Launch
            # separately compares that receipt with the current runtime.
            fingerprint = read_record(admission)['fingerprint']
            validate_admission(admission, fingerprint)
        except (OSError, ValueError, KeyError, TypeError):
            state = 'completion_evidence_invalid'
            errors.append('qualification evidence verification failed')
    latest = None
    jobs = launch['jobs']
    if launch.get('mode') == 'qualify' and (directory / 'control-jobs.json').exists():
        jobs = read_record(directory / 'control-jobs.json')['jobs']
    for job in jobs:
        root = Path(job)
        for path in sorted(root.glob('*/result.json')):
            try:
                result = json.loads(path.read_text())
                error = result.get('exception_info') or {}
                trials.append({'path': str(path.parent), 'exception_type': error.get('exception_type'),
                               'reward': (result.get('verifier_result') or {}).get('rewards')})
                latest = max(latest or 0, path.stat().st_mtime)
            except (OSError, ValueError, AttributeError):
                errors.append(str(path))
        for path in root.glob('*/verifier/sandbox-gateway.jsonl'):
            try:
                for line in path.read_text().splitlines():
                    event = json.loads(line)
                    if event.get('event') == 'request':
                        outcomes[event['outcome']] += 1
            except (OSError, ValueError, KeyError, AttributeError):
                errors.append(str(path))
    log = directory / 'campaign.log'
    return {'schema': SCHEMA, 'state': state, 'mode': launch.get('mode','run'), 'host': launch['host'], 'session': launch['session'],
            'directory': str(directory), 'manifest_sha256': launch['manifest_sha256'],
            'supervisor_active': active, 'tmux_session_present': tmux_alive,
            'log': str(log), 'last_log_update_unix': log.stat().st_mtime if log.exists() else None,
            'last_trial_result_unix': latest, 'trial_results': trials,
            'transport_outcomes': dict(outcomes), 'unreadable_evidence': errors,
            'completion': finished}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='obench campaign', description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    launch = sub.add_parser('launch', help='launch a pinned canonical suite in tmux')
    launch.add_argument('suite')
    launch.add_argument('--harbor-binary', default='harbor')
    launch.add_argument('--admission', help='passing local runtime admission JSON for repair suites')
    qualify_parser = sub.add_parser('qualify', help='run offline and authenticated controls in tmux')
    qualify_parser.add_argument('suite')
    qualify_parser.add_argument('--harbor-binary', default='harbor')
    qualify_parser.add_argument('--auth-file', required=True, help='explicit local Codex OAuth file; read-only temporary staging')
    status = sub.add_parser('status', help='read local supervisor and Harbor evidence')
    status.add_argument('directory')
    execute_parser = sub.add_parser('_execute', help=argparse.SUPPRESS)
    execute_parser.add_argument('directory')
    args = parser.parse_args(argv)
    try:
        if args.command in ('launch', 'qualify'):
            directory = launch_campaign(args.suite, harbor_binary=args.harbor_binary,
                                        admission=getattr(args,'admission',None), qualify=args.command=='qualify',
                                        auth_file=getattr(args,'auth_file',None))
            print(json.dumps({'directory': str(directory), 'status_command': ['obench','campaign','status',str(directory)]}))
        elif args.command == 'status':
            print(json.dumps(campaign_status(args.directory), indent=2, sort_keys=True))
        else:
            return execute(args.directory)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'obench campaign: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
