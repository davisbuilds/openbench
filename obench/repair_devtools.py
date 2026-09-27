"""Developer workflows available inside the source-free repair runtime.

This module contains no task tests, oracle, or original repository history.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def run(*args, cwd=None, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=60)
    if result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stdout[-8000:]}')
    return result.stdout.strip()


def initialize_git(workspace: Path, git_dir: Path):
    """Create a synthetic baseline containing only the supplied workspace.

    Keep Git objects outside /app so ordinary diffs do not inflate exported
    candidate archives. The regular .git pointer is never submitted for grading.
    """
    workspace, git_dir = workspace.resolve(), git_dir.resolve()
    if any(p.name == '.git' for p in workspace.rglob('*')) or git_dir.exists():
        raise ValueError('supplied Git history is forbidden')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_DATE='2000-01-01T00:00:00Z', GIT_COMMITTER_DATE='2000-01-01T00:00:00Z')
    with tempfile.TemporaryDirectory() as template:
        run('git', 'init', '--quiet', '--initial-branch=repair', '--template=' + template,
            '--separate-git-dir=' + str(git_dir), str(workspace), env=env)
    def git(*args):
        return run('git', '-C', str(workspace), *args, env=env)
    git('config', 'user.name', 'OpenBench')
    git('config', 'user.email', 'openbench')
    git('config', 'commit.gpgsign', 'false')
    git('config', 'core.hooksPath', '/dev/null')
    git('add', '--force', '.')
    git('commit', '--quiet', '--allow-empty', '-m', 'Supplied task baseline')


def check():
    """Exercise tools offline as the solver, including compilers and tests."""
    commands = {
        'git': ('git', '--version'), 'rg': ('rg', '--version'),
        'bash': ('bash', '--version'), 'jq': ('jq', '--version'),
        'python': ('python3', '--version'), 'pytest': ('pytest', '--version'),
        'node': ('node', '--version'), 'npm': ('npm', '--version'),
        'npx': ('npx', '--version'), 'pnpm': ('pnpm', '--version'),
        'typescript': ('tsc', '--version'), 'tsx': ('tsx', '--version'),
        'make': ('make', '--version'), 'gcc': ('gcc', '--version'),
        'g++': ('g++', '--version'), 'curl': ('curl', '--version'),
    }
    versions = {key: run(*command).splitlines()[0] for key, command in commands.items()}
    with tempfile.TemporaryDirectory(prefix='developer-check-') as directory:
        root = Path(directory)
        app = root / 'app'
        app.mkdir()
        (app / 'example.py').write_text('value = 1\n')
        initialize_git(app, root / 'history')
        assert run('git', 'status', '--porcelain', cwd=app) == ''
        assert run('git', 'rev-list', '--count', 'HEAD', cwd=app) == '1'
        assert run('git', 'remote', cwd=app) == ''
        (app / 'example.py').write_text('value = 2\n')
        assert '+value = 2' in run('git', 'diff', cwd=app)
        assert run('rg', 'value = 2', 'example.py', cwd=app) == 'value = 2'
        (app / 'test_example.py').write_text('from example import value\ndef test_value():\n    assert value == 2\n')
        run('python3', '-m', 'pytest', '-q', cwd=app)
        (app / 'example.ts').write_text('const value: number = 2; if (value !== 2) throw Error("bad");\n')
        (app / 'package.json').write_text('{"name":"developer-workflow-check","private":true}\n')
        # No project lockfile or install: binaries and packages are preinstalled.
        run('pnpm', 'exec', 'tsc', '--ignoreConfig', '--noEmit', '--skipLibCheck', 'example.ts', cwd=app)
        run('pnpm', 'exec', 'tsx', 'example.ts', cwd=app)
        run('npx', '--no-install', 'tsc', '--version', cwd=app)
        (app / 'probe.c').write_text('int main(void) { return 0; }\n')
        (app / 'Makefile').write_text('all:\n\t$(CC) probe.c -o probe\n')
        run('make', cwd=app)
        run(str(app / 'probe'), cwd=app)
        run('bash', '-c', "jq -n -e '{ok:true}.ok'", cwd=app)
        run('ps', '-o', 'pid=', '-p', str(os.getpid()))
        scratch = app / 'scratch'
        scratch.mkdir()
        (scratch / 'temporary').write_text('disposable')
        run('rm', '-r', str(scratch))
        assert not scratch.exists()
    return {'schema': 1, 'versions': versions, 'workflows': ['git-baseline-diff', 'search',
            'python-tests', 'typescript-check-run', 'native-build', 'json', 'processes', 'cleanup']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'check'))
    args = parser.parse_args()
    if args.action == 'init':
        initialize_git(Path('/app'), Path('/tmp/openbench-workspace.git'))
    else:
        print(json.dumps(check(), sort_keys=True))


if __name__ == '__main__':
    main()
