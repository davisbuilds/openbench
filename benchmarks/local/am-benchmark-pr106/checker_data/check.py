"""Hidden verifier; grades behavior in a disposable copy with host-native deps."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

workspace, task = (Path(p).resolve() for p in sys.argv[1:])
deps_env = os.environ.get('AGENTMONITOR_DEPS')
if not deps_env:
    print('SKIP: set AGENTMONITOR_DEPS to a host-compatible node_modules directory', file=sys.stderr)
    sys.exit(77)
deps = Path(deps_env).resolve()
if not deps.is_dir() or not shutil.which('node'):
    print('SKIP: node or AGENTMONITOR_DEPS unavailable', file=sys.stderr)
    sys.exit(77)
probe = subprocess.run(['node', '-e', "const DB=require(process.argv[1]);const db=new DB(':memory:');db.prepare('SELECT 1').get();db.close();require.resolve(process.argv[2]);", str(deps / 'better-sqlite3'), str(deps / 'tsx/package.json')], capture_output=True, text=True)
if probe.returncode:
    print('SKIP: native SQLite or tsx dependency preflight failed; provision host-compatible AGENTMONITOR_DEPS', file=sys.stderr)
    print(probe.stderr, file=sys.stderr)
    sys.exit(77)
if not (workspace / 'src').is_dir():
    print('SCORE: 0.0')
    sys.exit(1)
with tempfile.TemporaryDirectory(prefix='obench-am106-check-') as tmp:
    root = Path(tmp)
    shutil.copytree(workspace / 'src', root / 'src')
    # Freeze execution metadata; implementation is the submitted src tree.
    for name in ['package.json', 'tsconfig.json']:
        shutil.copyfile(task / 'workspace' / name, root / name)
    (root / 'node_modules').symlink_to(deps, target_is_directory=True)
    (root / 'tests').mkdir()
    shutil.copyfile(task / 'checker_data/golden.test.ts', root / 'tests/golden.test.ts')
    outcomes = {}
    for name, pattern, expected in [('identity', r'^ID[1-4] ', 4), ('migration', r'^MIG[1-2] ', 2), ('coverage', r'^GRID[1-2] ', 2), ('guards', r'^GUARD[1-2] ', 2)]:
        try:
            result = subprocess.run(['node', '--import', 'tsx', '--test', '--test-reporter=tap', '--test-name-pattern=' + pattern, 'tests/golden.test.ts'], cwd=root, capture_output=True, text=True, timeout=90)
            counts = {k: int(v) for k,v in re.findall(r'^# (tests|pass|fail|cancelled|skipped) (\d+)$', result.stdout, re.M)}
            success = result.returncode == 0 and counts == {'tests':expected, 'pass':expected, 'fail':0, 'cancelled':0, 'skipped':0}
            outcomes[name] = success
            print(json.dumps({'bucket':name, 'pass':success, 'returncode':result.returncode, 'counts':counts}), file=sys.stderr)
            if not success:
                print(result.stdout, file=sys.stderr)
                print(result.stderr, file=sys.stderr)
        except subprocess.TimeoutExpired:
            outcomes[name] = False
            print(f'{name}: timed out', file=sys.stderr)
    score = sum(outcomes[k] for k in ['identity','migration','coverage']) / 3 if outcomes['guards'] else 0
    print(f'SCORE: {score:.4f}')
    sys.exit(0 if score == 1 else 1)
