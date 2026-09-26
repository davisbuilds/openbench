#!/usr/bin/env python3
"""Offline behavioral admission controls; source tasks are never modified.

Usage: python3 validate_controls.py --output /absolute/local/results/directory
Requires the same host-native dependencies as checker.sh. No model calls.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

TASK = Path(__file__).resolve().parent
V1 = TASK.with_name('am-benchmark-pr106')
FILES = ['src/import/benchmark.ts', 'src/db/queries.ts', 'src/db/schema.ts', 'src/db/v2-queries.ts']


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise AssertionError(f'expected one mutation site in {path.name}')
    path.write_text(text.replace(old, new))


def alternate(workspace, variant):
    if variant in ('absolute-identity', 'alternative-combined'):
        replace_once(workspace / FILES[0], "import { readFileSync } from 'node:fs';",
                     "import { readFileSync, realpathSync } from 'node:fs';")
        replace_once(workspace / FILES[0],
                     "const legacyStudy = parentDir || path.basename(resolved).replace(/\\.[^.]*$/, '');",
                     'const legacyStudy = path.dirname(realpathSync(resolved));')
    if variant in ('startup-migration', 'alternative-combined'):
        schema = workspace / 'src/db/schema.ts'
        replace_once(schema, '    if (current < 5) deleteLegacyBenchmarkRows(db);\n', '')
        replace_once(schema, '  runDataMigrations(db);', '''  // An equally valid one-time startup mechanism independent of user_version.
  db.exec('CREATE TABLE IF NOT EXISTS benchmark_upgrade_receipts (name TEXT PRIMARY KEY)');
  if (!db.prepare('SELECT 1 FROM benchmark_upgrade_receipts WHERE name = ?').get('identity')) {
    db.transaction(() => {
      deleteLegacyBenchmarkRows(db);
      db.prepare('INSERT INTO benchmark_upgrade_receipts (name) VALUES (?)').run('identity');
    })();
  }
  runDataMigrations(db);''')
    if variant in ('summary-and-session', 'alternative-combined'):
        replace_once(workspace / FILES[0], '      session_id: eventId,', '      session_id: runId,')
        replace_once(workspace / FILES[3], '    expected_trials,', '    expected_trials: distinctTrials.size,')


def check(workspace, task, output, name):
    result = subprocess.run(['bash', str(task / 'checker.sh')], cwd=workspace,
                            env={**os.environ, 'TASK_DIR': str(task)},
                            text=True, capture_output=True, timeout=400)
    (output / f'{name}.stdout').write_text(result.stdout)
    (output / f'{name}.stderr').write_text(result.stderr)
    score_lines = [line for line in result.stdout.splitlines() if line.startswith('SCORE:')]
    score = float(score_lines[-1].split(':', 1)[1]) if len(score_lines) == 1 else None
    buckets = [json.loads(line) for line in result.stderr.splitlines() if line.startswith('{"bucket":')]
    return {'exit': result.returncode, 'score': score, 'buckets': buckets}


def tree_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    before = tree_hashes(V1)
    variants = {
        'buggy': ([], 0.0),
        'reference': (FILES, 1.0),
        'identity-only': (FILES[:2], 0.3333),
        'migration-only': ([FILES[2]], 0.3333),
        'coverage-only': ([FILES[3]], 0.3333),
        'identity-coverage': (FILES[:2] + [FILES[3]], 0.6667),
        **{name: (FILES, 1.0) for name in ['absolute-identity', 'startup-migration',
                                        'summary-and-session', 'alternative-combined']},
    }
    results = {}
    for name, (overlay, expected) in variants.items():
        with tempfile.TemporaryDirectory(prefix='am106-v2-control-') as directory:
            workspace = Path(directory)
            shutil.copytree(TASK / 'workspace', workspace, dirs_exist_ok=True)
            for relative in overlay:
                shutil.copy2(TASK / 'solution' / relative, workspace / relative)
            alternate(workspace, name)
            result = check(workspace, TASK, output, name)
            results[name] = result
            print(name, result['exit'], result['score'], flush=True)
            assert result['score'] == expected and result['exit'] == (0 if expected == 1 else 1), result
            if name in ['absolute-identity', 'startup-migration', 'summary-and-session', 'alternative-combined']:
                old = check(workspace, V1, output, name + '-v1')
                results[name + '-v1'] = old
                assert old['score'] is not None and old['score'] < 1 and old['exit'] == 1, old
    assert tree_hashes(V1) == before, 'v1 task changed during controls'
    report = {'status': 'passed', 'v1_unchanged': True, 'checks': results,
              'task_inputs': {key: value for key, value in tree_hashes(TASK).items()
                              if key.startswith(('workspace/', 'solution/', 'checker_data/'))
                              or key in ('instruction.md', 'checker.sh')}}
    (output / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
