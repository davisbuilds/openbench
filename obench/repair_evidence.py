"""Private, durable operator evidence. Never staged into a solver container."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import uuid


def write_json(path, value, *, exclusive=False):
    """Publish a complete private record or leave the previous record intact."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.record-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Operation:
    def __init__(self, directory, kind):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.record = {'schema': 1, 'kind': 'repair-operation', 'operation': kind,
                       'id': uuid.uuid4().hex, 'host': socket.gethostname(),
                       'started_at': stamp(), 'status': 'running', 'stages': [],
                       'evidence': [], 'blockers': []}
        self.started = time.monotonic()
        self.lock = None

    def save(self):
        self.record['elapsed_seconds'] = round(time.monotonic() - self.started, 3)
        write_json(self.directory / 'operation.json', self.record)

    def __enter__(self):
        self.lock = (self.directory / 'execution.lock').open('x')
        os.chmod(self.directory / 'execution.lock', 0o600)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            self.save()
        except BaseException:
            self.lock.close()
            raise
        return self

    def __exit__(self, kind, exc, tb):
        try:
            if exc is not None:
                self.record.update(status='incomplete', blockers=[{
                    'code': kind.__name__, 'message': str(exc)[:2000]}])
            elif self.record['status'] == 'running':
                self.record.update(status='incomplete', blockers=[{
                    'code': 'unfinished', 'message': 'operation did not publish a final result'}])
            self.record['finished_at'] = stamp()
            self.save()
        finally:
            self.lock.close()

    @contextmanager
    def stage(self, name):
        record = {'id': len(self.record['stages']), 'name': name, 'status': 'running'}
        self.record['stages'].append(record)
        self.save()
        started = time.monotonic()
        try:
            yield
        except BaseException as exc:
            record.update(status='incomplete', error_type=type(exc).__name__, error=str(exc)[:2000])
            raise
        else:
            record['status'] = 'completed'
        finally:
            record['elapsed_seconds'] = round(time.monotonic() - started, 3)
            self.save()

    def artifact(self, kind, value):
        # Candidate/control IDs are labels, never filesystem paths.
        path = self.directory / f'{len(self.record["evidence"]):04d}-{kind}.json'
        write_json(path, value, exclusive=True)
        item = {'path': path.name, 'sha256': digest(path), 'kind': kind}
        if kind == 'control':
            item.update(control_id=value['id'], role=value['role'],
                        score=value['result']['grading']['score'],
                        failed_checks=[check['id'] for check in value['result']['grading']['checks']
                                       if check['pass'] is not True])
        self.record['evidence'].append(item)
        self.save()
        return path

    def finish(self, result):
        self.artifact('result', result)
        self.record['status'] = 'failed' if result.get('status') == 'failed' or result.get('solved') is False else 'passed'
        self.record['blockers'] = [{'code': 'quality_control', 'message': text}
                                   for text in result.get('findings', [])]


def status(directory):
    """A diagnostic view, not permission to launch or reuse a saved verdict."""
    directory = Path(directory).resolve()
    record = json.loads((directory / 'operation.json').read_text())
    if record['schema'] != 1 or record['kind'] != 'repair-operation':
        raise ValueError('unsupported repair operation')
    if record['host'] != socket.gethostname():
        raise ValueError('inspect operation status on its execution host')
    with (directory / 'execution.lock').open('r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active = False
        except BlockingIOError:
            active = True
        # Reread after checking the lock so completion cannot race a stale read.
        record = json.loads((directory / 'operation.json').read_text())
        invalid = []
        for item in record['evidence']:
            name = item['path']
            path = directory / name
            if (Path(name).name != name or path.is_symlink() or not path.is_file()
                    or digest(path) != item['sha256']):
                invalid.append(name)
        if invalid:
            record['status'] = 'evidence_invalid'
            record['blockers'].append({'code': 'evidence_changed', 'paths': invalid})
        elif record['status'] == 'running' and not active:
            record['status'] = 'interrupted'
            record['blockers'].append({'code': 'interrupted', 'message': 'worker exited without a final record'})
    return {**record, 'active': active, 'directory': str(directory),
            'scope': 'diagnostic evidence only; fresh admission replay is still required'}
