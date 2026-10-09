"""Conservative, local launch-time capacity selection; never a live scheduler."""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

GIB = 1024 ** 3


def output(*argv):
    return subprocess.check_output(argv, text=True, timeout=15).strip()


def memory_bytes(value):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB|kB|MB|GB|TB)', value.strip())
    if not match:
        raise ValueError('unrecognized Docker memory quantity')
    scales = {'B':1, 'KiB':1024, 'MiB':1024**2, 'GiB':GIB, 'TiB':1024**4,
              'kB':1000, 'MB':1000**2, 'GB':1000**3, 'TB':1000**4}
    return int(float(match[1]) * scales[match[2]])


def sample(directory='.'):
    values = {'host_cpus':os.cpu_count(), 'load_one':os.getloadavg()[0],
              'disk_free_bytes':shutil.disk_usage(directory).free}
    errors = []
    try:
        if sys.platform == 'darwin':
            total = int(output('sysctl', '-n', 'hw.memsize'))
            query = output('memory_pressure', '-Q')
            percent = int(re.search(r'System-wide memory free percentage:\s*(\d+)%', query)[1])
            if not 0 <= percent <= 100: raise ValueError('invalid memory percentage')
            values['host_available_bytes'] = total * percent // 100
        elif sys.platform.startswith('linux'):
            text = Path('/proc/meminfo').read_text()
            values['host_available_bytes'] = int(re.search(r'^MemAvailable:\s*(\d+) kB$', text, re.M)[1])*1024
        else:
            raise ValueError('unsupported host memory probe')
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        errors.append('host_memory_unavailable')
    try:
        info = json.loads(output('docker', 'info', '--format', '{{json .}}'))
        rows = [json.loads(line) for line in output('docker', 'stats', '--no-stream', '--format', '{{json .}}').splitlines()]
        values.update(docker_cpus=info['NCPU'], docker_total_bytes=info['MemTotal'],
                      docker_used_bytes=sum(memory_bytes(row['MemUsage'].split('/')[0]) for row in rows),
                      docker_cpu_cores=sum(float(row['CPUPerc'].removesuffix('%'))/100 for row in rows))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        errors.append('docker_capacity_unavailable')
    values['probe_errors'] = errors
    return values


def decide(values):
    def number(key):
        value = values.get(key)
        return value if type(value) in (int,float) and math.isfinite(value) and value >= 0 else None
    required = ('host_cpus','load_one','host_available_bytes','docker_cpus',
                'docker_total_bytes','docker_used_bytes','docker_cpu_cores','disk_free_bytes')
    reasons = ['unknown:' + key for key in required if number(key) is None]
    reasons += list(values.get('probe_errors', []))
    if not reasons:
        checks = {
            'host_cpu_headroom':values['host_cpus'] >= 4 and values['load_one'] <= values['host_cpus']*.75,
            'host_memory_headroom':values['host_available_bytes'] >= 6*GIB,
            'docker_cpu_headroom':values['docker_cpus'] >= 4 and values['docker_cpu_cores'] <= values['docker_cpus']*.75,
            # Two 2-GiB solver/broker pairs plus 2 GiB of VM/worker headroom.
            'docker_memory_headroom':values['docker_total_bytes']-values['docker_used_bytes'] >= 10*GIB,
            'disk_headroom':values['disk_free_bytes'] >= 20*GIB,
        }
        reasons = [key for key, ok in checks.items() if not ok]
    return {'schema':1, 'policy':'two-trial-capacity-v1', 'host':socket.gethostname(),
            'sampled_at':datetime.now(timezone.utc).isoformat(),
            'concurrency':1 if reasons else 2, 'reasons':reasons,
            'observed':{key: number(key) for key in required},
            'scope':'launch-time snapshot; no mid-run throttling or capacity reservation'}


def require_capacity(compiled):
    if compiled.suite.run.concurrency <= 1:
        return None
    decision = decide(sample(compiled.suite.project_root))
    if compiled.suite.run.concurrency > decision['concurrency']:
        raise ValueError('parallel capacity unavailable: ' + ', '.join(decision['reasons']) +
                         '; prepare a new serial suite and qualify that treatment; existing intent is unchanged')
    return decision


def prepare(source, destination):
    from .suite_run import compile_suite
    from .campaign import write_record
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source.parent.resolve() != destination.parent.resolve():
        raise ValueError('prepared suite must be in the same directory to preserve path resolution')
    receipt = destination.with_suffix('.capacity.json')
    if destination.exists() or destination.is_symlink() or receipt.exists() or receipt.is_symlink():
        raise FileExistsError('prepared suite or capacity receipt already exists')
    compiled = compile_suite(source)
    decision = decide(sample(compiled.suite.project_root))
    raw = source.read_bytes()
    text = raw.decode()
    pattern = r'(?ms)(^\[run\]\s*\n)(.*?)(?=^\[|\Z)'
    def replace_run(match):
        body, count = re.subn(r'(?m)^concurrency\s*=.*$', 'concurrency = '+str(decision['concurrency']), match[2])
        if count != 1: raise ValueError('prepare requires one plain concurrency key in [run]')
        return match[1]+body
    text, count = re.subn(pattern, replace_run, text)
    if count != 1: raise ValueError('prepare requires one [run] table')
    with destination.open('x') as stream:
        os.chmod(destination, 0o600)
        stream.write(text)
    try:
        selected = compile_suite(destination)
        if source.read_bytes() != raw: raise ValueError('source suite changed during preparation')
        write_record(receipt, {**decision, 'source_suite':str(source),
                     'source_sha256':hashlib.sha256(raw).hexdigest(),
                     'suite':str(destination), 'manifest_sha256':selected.manifest_sha256})
    except Exception:
        destination.unlink()
        raise
    return {'suite':str(destination), 'concurrency':decision['concurrency'],
            'reasons':decision['reasons'], 'capacity_receipt':str(receipt),
            'launched':False, 'next':'qualify the selected suite before campaign launch'}
