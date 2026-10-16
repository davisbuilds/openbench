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
    """Create synthetic history from the supplied workspace and optional diff.

    Keep Git objects outside /app so ordinary diffs do not inflate exported
    candidate archives. The regular .git pointer is never submitted for grading.
    """
    workspace, git_dir = workspace.resolve(), git_dir.resolve()
    if any(p.name == '.git' for p in workspace.rglob('*')) or git_dir.exists():
        raise ValueError('supplied Git history is forbidden')
    review = workspace / '.openbench-review.json'
    if review.exists():
        if review.is_symlink() or json.loads(review.read_text()) != {'schema': 1, 'patch': 'review.patch'}:
            raise ValueError('invalid review Git descriptor')
        patch = workspace / 'review.patch'
        if patch.is_symlink() or not patch.is_file() or not patch.stat().st_size:
            raise ValueError('review requires a regular nonempty review.patch')
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
    if review.exists():
        # Reverse only the index; never execute repository code or import objects,
        # remotes, hooks, identities or messages from the historical repository.
        candidate_tree = git('write-tree')
        git('apply', '--reverse', '--cached', '--whitespace=nowarn', '--', 'review.patch')
        base_tree = git('write-tree')
        if base_tree == candidate_tree:
            raise ValueError('review diff has no changes')
        base = git('commit-tree', base_tree, '-m', 'Supplied review base')
        candidate = git('commit-tree', candidate_tree, '-p', base, '-m', 'Supplied review candidate')
        git('update-ref', 'refs/heads/review-base', base)
        git('update-ref', 'refs/heads/review-candidate', candidate)
        git('symbolic-ref', 'HEAD', 'refs/heads/review-candidate')
        git('read-tree', candidate_tree)
    else:
        git('commit', '--quiet', '--allow-empty', '-m', 'Supplied task baseline')


def initialize_dependencies(workspace: Path):
    """Writable local scaffolding, immutable offline packages; no install/hooks.

    Package-local tmp directories must be writable. Linking the whole directory
    to /opt makes ordinary packaging tests fail on the read-only image root.
    This generated directory is not part of the submitted source or Git baseline.
    """
    if not (workspace / 'package.json').is_file():
        return
    target = workspace / 'node_modules'
    if target.exists() or target.is_symlink():
        raise ValueError('supplied node_modules is forbidden')
    target.mkdir()
    # Node resolves the pinned packages through the existing /node_modules
    # ancestor. Keep /app/node_modules free of links so sealed workspace export
    # retains its strict regular-file-only boundary.


def initialize_workspace(workspace: Path, git_dir: Path):
    initialize_git(workspace, git_dir)
    exclude = git_dir / 'info/exclude'
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open('a') as f:
        f.write('\n/node_modules/\n')
    initialize_dependencies(workspace)


def check():
    """Exercise tools offline as the solver, including compilers and tests."""
    commands = {
        'git': ('git', '--version'), 'rg': ('rg', '--version'),
        'bash': ('bash', '--version'), 'jq': ('jq', '--version'),
        'python': ('python3', '--version'), 'pytest': ('pytest', '--version'),
        'node': ('node', '--version'), 'npm': ('npm', '--version'),
        'go': ('go', 'version'),
        'npx': ('npx', '--version'), 'pnpm': ('pnpm', '--version'),
        'typescript': ('tsc', '--version'), 'tsx': ('tsx', '--version'),
        'eslint': ('eslint', '--version'),
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
        review_app = root / 'review'
        review_app.mkdir()
        (review_app / 'example.py').write_text('value = 2\n')
        (review_app / 'review.patch').write_text(run('git', 'diff', cwd=app) + '\n')
        (review_app / '.openbench-review.json').write_text('{"schema":1,"patch":"review.patch"}')
        initialize_workspace(review_app, root / 'review-history')
        assert '+value = 2' in run('git', 'diff', 'review-base', 'HEAD', cwd=review_app)
        assert run('git', 'rev-list', '--count', 'HEAD', cwd=review_app) == '2'
        assert run('git', 'status', '--porcelain', cwd=review_app) == ''
        (app / 'test_example.py').write_text('from example import value\ndef test_value():\n    assert value == 2\n')
        run('python3', '-m', 'pytest', '-q', cwd=app)
        (app / 'go.mod').write_text('module example.test/workflow\n\ngo 1.26\n')
        (app / 'probe_test.go').write_text('package workflow\nimport "testing"\nfunc TestToolchain(t *testing.T) { if 1+1 != 2 { t.Fatal("arithmetic") } }\n')
        run('go', 'test', './...', cwd=app)
        (app / 'go.mod').unlink()
        (app / 'probe_test.go').unlink()
        (app / 'example.ts').write_text('const value: number = 2; if (value !== 2) throw Error("bad");\n')
        (app / 'package.json').write_text('{"name":"developer-workflow-check","private":true}\n')
        # No project lockfile or install: binaries and packages are preinstalled.
        run('pnpm', 'exec', 'tsc', '--ignoreConfig', '--noEmit', '--skipLibCheck', 'example.ts', cwd=app)
        run('pnpm', 'exec', 'tsx', 'example.ts', cwd=app)
        run('npx', '--no-install', 'tsc', '--version', cwd=app)
        # Prove the project's lint entry points work offline, including ESM
        # config imports and TypeScript parsing. A binary version alone misses
        # missing config/parser packages and npx's attempted registry fallback.
        (app / 'eslint.config.mjs').write_text(
            'import eslint from "@eslint/js";\n'
            'import tseslint from "typescript-eslint";\n'
            'export default [eslint.configs.recommended, ...tseslint.configs.recommended];\n')
        (app / 'lint.ts').write_text('export const value: number = 2;\n')
        run('pnpm', 'exec', 'eslint', 'lint.ts', cwd=app)
        run('npx', '--no-install', 'eslint', 'lint.ts', cwd=app)
        (app / 'lint.ts').write_text('const unused: number = 2;\n')
        rejected = subprocess.run(['pnpm', 'exec', 'eslint', 'lint.ts'], cwd=app,
                                  text=True, capture_output=True, timeout=60)
        if rejected.returncode != 1 or 'no-unused-vars' not in rejected.stdout:
            raise RuntimeError('lint negative control did not detect unused TypeScript')
        (app / 'probe.c').write_text('int main(void) { return 0; }\n')
        (app / 'Makefile').write_text('all:\n\t$(CC) probe.c -o probe\n')
        run('make', cwd=app)
        run(str(app / 'probe'), cwd=app)
        run('bash', '-c', "jq -n -e '{ok:true}.ok'", cwd=app)
        if run('bash', '-lc', 'printf "%s " {alpha,beta}').strip() != 'alpha beta':
            raise RuntimeError('Bash brace expansion unavailable')
        initialize_dependencies(app)
        run('node', '-e', "const fs=require('fs'); const p=fs.mkdtempSync('node_modules/.tmp-cli-'); fs.rmSync(p,{recursive:true}); require('better-sqlite3')", cwd=app)
        (app / 'input.css').write_text('@import "tailwindcss";\n@source inline("text-red-500");\n')
        run('pnpm', 'exec', 'tailwindcss', '-i', 'input.css', '-o', 'output.css', cwd=app)
        if '.text-red-500' not in (app / 'output.css').read_text():
            raise RuntimeError('CSS build did not emit the requested class')
        run('ps', '-o', 'pid=', '-p', str(os.getpid()))
        scratch = app / 'scratch'
        scratch.mkdir()
        (scratch / 'temporary').write_text('disposable')
        run('rm', '-r', str(scratch))
        assert not scratch.exists()
    return {'schema': 1, 'versions': versions, 'workflows': ['git-baseline-diff', 'git-review-diff', 'search',
            'python-tests', 'go-tests', 'typescript-check-run', 'typescript-lint', 'native-build', 'json', 'processes', 'cleanup']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'check'))
    args = parser.parse_args()
    if args.action == 'init':
        initialize_workspace(Path('/app'), Path('/tmp/openbench-workspace.git'))
    else:
        print(json.dumps(check(), sort_keys=True))


if __name__ == '__main__':
    main()
