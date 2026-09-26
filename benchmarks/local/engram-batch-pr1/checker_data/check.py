"""Offline, uncached behavioral oracle. Candidate output is never a score source."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SPEC = json.loads((HERE / 'tests.json').read_text())

def fail(message):
    print(message, file=sys.stderr)
    print('SCORE: 0.0')
    raise SystemExit(1)

def skip(message):
    print('SKIP: ' + message, file=sys.stderr)
    raise SystemExit(77)

if not shutil.which('go'):
    skip('Go toolchain required')
env = dict(os.environ, GOPROXY='off', GOSUMDB='off', GOTOOLCHAIN='local', GOWORK='off', GOFLAGS='', GOENV='off')
version = subprocess.run(['go', 'env', 'GOVERSION'], env=env, text=True, capture_output=True)
m = re.search(r'go(\d+)\.(\d+)', version.stdout)
if version.returncode or not m or tuple(map(int, m.groups())) < (1, 26):
    skip('local Go >=1.26 required; automatic toolchain downloads disabled')
with tempfile.TemporaryDirectory(prefix='engram-oracle-') as temp:
    root = Path(temp)
    provision = root / 'provision'
    provision.mkdir()
    for name in ('go.mod', 'go.sum'):
        shutil.copyfile(HERE / 'pinned' / name, provision / name)
    deps = subprocess.run(['go', 'mod', 'download', '-json', 'gopkg.in/yaml.v3@v3.0.1'], cwd=provision, env=env, capture_output=True, text=True, timeout=30)
    if deps.returncode:
        skip('pinned gopkg.in/yaml.v3@v3.0.1 not available in local module cache; provision before evaluation')
    work = root / 'work'
    shutil.copytree(Path.cwd(), work, ignore=shutil.ignore_patterns('.git', '*_test.go'))
    package = work / 'internal' / 'curate'
    if not package.is_dir():
        fail('FAIL: expected implementation package missing')
    shutil.copyfile(HERE / 'upstream/internal/curate/curate_test.go', package / 'oracle_upstream_test.go')
    shutil.copyfile(HERE / 'behavior_test.go', package / 'oracle_behavior_test.go')
    try:
        run = subprocess.run(['go', 'test', '-json', '-count=1', '-timeout=45s', './internal/curate'], cwd=work, env=env, capture_output=True, text=True, timeout=75)
    except subprocess.TimeoutExpired:
        fail('FAIL: behavioral test timeout')
    events = []
    for line in run.stdout.splitlines():
        try: event = json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(event, dict): events.append(event)
    expected = set(SPEC['controls'] + sum(SPEC['buckets'].values(), []))
    states = {e['Test']: e['Action'] for e in events if e.get('Test') in expected and e.get('Action') in ('pass', 'fail', 'skip')}
    if set(states) != expected or any(v == 'skip' for v in states.values()):
        print(run.stderr, file=sys.stderr)
        fail('FAIL: missing, skipped, or unbuildable required tests: ' + str(sorted(expected - set(states))))
    failures = {k for k,v in states.items() if v == 'fail'}
    package_end = [e.get('Action') for e in events if not e.get('Test') and e.get('Action') in ('pass','fail')]
    if not package_end or (run.returncode == 0) != (not failures) or package_end[-1] != ('fail' if failures else 'pass'):
        fail('FAIL: inconsistent process/package/test result')
    controls = all(states[n] == 'pass' for n in SPEC['controls'])
    passed = 0
    print('controls: ' + ('PASS' if controls else 'FAIL'))
    for name, tests in SPEC['buckets'].items():
        ok = controls and all(states[n] == 'pass' for n in tests)
        passed += ok
        print(name + ': ' + ('PASS' if ok else 'FAIL'))
    for name in sorted(failures):
        print('FAIL TEST: ' + name, file=sys.stderr)
    print('SCORE: %.4f' % (passed / len(SPEC['buckets'])))
    raise SystemExit(0 if passed == len(SPEC['buckets']) else 1)
