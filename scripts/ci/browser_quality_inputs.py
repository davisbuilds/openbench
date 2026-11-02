#!/usr/bin/env python3
"""Decide whether a pull request can skip the Harbor browser-quality lane.

The lane's inputs are derived from source at both ends of the comparison, not
from a maintained file list:

1. Steps in the Harbor workflow's integration job that can run for the
   browser-quality lane name its entry scripts, task, and dependency locks.
   A step is excluded only when its condition is exactly
   ``matrix.lane == '<another lane>'``; any other condition counts as
   applicable.
2. Python sources are followed through their imports, as
   ``obench/evidence_identity.py`` does: literal ``obench.*`` names in strings
   count, and reaching the adapter, Harbor-agent or oracle package includes
   every member, because those families also load by name. Bare imports
   resolve to scripts on the lane's added ``sys.path`` entries.
3. String literals in that closure that name a tracked path add it (build
   contexts, Dockerfiles, package locks, task directories). A literal naming
   a file beside the module, or a non-Python file under ``obench/``, adds it
   too, which covers ``Path(__file__).parent / 'name'`` lookups.
4. The workflow and this classifier are always inputs.

The input set is the union for the merge base and the head, so a deleted or
newly referenced file is still matched. The lane runs when any file the pull
request changes is an input. Every error, unknown revision, shallow history or
empty comparison selects running the lane; only a completed comparison with no
matching path selects a skip.

Prints one JSON report line, then ``run=true`` or ``run=false`` as the last
line. Exit status is 0 whenever a decision was printed, including the
fail-closed decision to run after an error.
"""
import argparse
import ast
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

WORKFLOW = '.github/workflows/harbor-integration.yml'
JOB = 'integration'
LANE = 'browser-quality'
CLASSIFIER = 'scripts/ci/browser_quality_inputs.py'
# Families that also load by filename or registry string; mirrors
# obench.evidence_identity.DYNAMIC_FAMILIES.
DYNAMIC_FAMILIES = ('obench/adapters', 'obench/harbor_agents', 'obench/repair_oracles')
# Directories the lane's scripts prepend to sys.path for bare imports.
SCRIPT_PATHS = ('scripts/ci', 'scripts/local')
PATH_TOKEN = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+')
MODULE_TOKEN = re.compile(r'\bobench(?:\.[A-Za-z_]\w*)+')
NAME_TOKEN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*')
SHA = re.compile(r'[0-9a-f]{40}')
OTHER_LANE_ONLY = re.compile(r"""^\$?\{?\{?\s*matrix\.lane\s*==\s*'([A-Za-z0-9_-]+)'\s*\}?\}?$""")


class Tree:
    """Tracked files of one commit, read through Git without a checkout."""

    def __init__(self, repo, rev):
        self.repo, self.rev = repo, rev
        self._index(git(repo, 'ls-tree', '-r', '-z', '--name-only', rev).split('\0'))

    def _index(self, paths):
        self.files = frozenset(p for p in paths if p)
        if not self.files:
            raise ValueError(f'no files at {self.rev}')
        self.dirs = frozenset(str(parent) for p in self.files for parent in PurePosixPath(p).parents
                              if str(parent) != '.')

    def read(self, path):
        return git(self.repo, 'show', f'{self.rev}:{path}')


class WorkTree(Tree):
    """Tracked and untracked, non-ignored files of a working tree."""

    def __init__(self, repo):
        self.repo, self.rev = repo, 'working tree'
        listing = git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
        self._index(p for p in listing.split('\0') if p and (Path(repo)/p).is_file())

    def read(self, path):
        return (Path(self.repo)/path).read_text()


def git(repo, *args):
    return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True,
                          text=True).stdout


def lane_steps(text):
    """Return (text, lane_specific) for each integration step the lane can run."""
    lines = text.splitlines()
    try:
        start = lines.index('jobs:')
        job = lines.index(f'  {JOB}:', start)
    except ValueError as error:
        raise ValueError(f'{WORKFLOW} has no {JOB} job') from error
    end = next((i for i in range(job+1, len(lines)) if re.match(r'^  \S', lines[i])), len(lines))
    body = lines[job+1:end]
    try:
        first = body.index('    steps:')
    except ValueError as error:
        raise ValueError(f'{JOB} job has no steps') from error
    steps, current = [], None
    for line in body[first+1:]:
        if line.startswith('      - '):
            current = [line]
            steps.append(current)
        elif current is not None:
            current.append(line)
    if not steps:
        raise ValueError(f'{JOB} job has no parseable steps')
    applicable, specific = [], 0
    for step in steps:
        conditions = [re.sub(r'^\s*-?\s*if:\s*', '', line).strip() for line in step
                      if re.match(r'^      (- |  )if:', line)]
        match = OTHER_LANE_ONLY.match(conditions[0]) if len(conditions) == 1 else None
        if match and match[1] != LANE:
            continue
        specific += bool(match)
        applicable.append(('\n'.join(step), bool(match)))
    # Without a step conditioned exactly on this lane, the job layout is not
    # the one this parser understands (or predates the lane): fail closed.
    if not specific:
        raise ValueError(f"no {JOB} step is conditioned on matrix.lane == '{LANE}'")
    return applicable


def modules_of(tree):
    modules = {}
    for path in tree.files:
        if path.endswith('.py') and path.startswith('obench/'):
            name = '.'.join(PurePosixPath(path).with_suffix('').parts).removesuffix('.__init__')
            modules[name] = path
    return modules


def inputs(tree):
    """Return (files, directories) the lane reads at this commit."""
    modules = modules_of(tree)
    files, directories, pending = {WORKFLOW, CLASSIFIER}, set(), []
    if WORKFLOW not in tree.files:
        raise ValueError(f'{WORKFLOW} is missing at {tree.rev}')
    # Files a module can open by bare name: its siblings, and package data.
    siblings, package_data = {}, {}
    for path in tree.files:
        name = PurePosixPath(path).name
        siblings.setdefault((str(PurePosixPath(path).parent), name), []).append(path)
        if path.startswith('obench/') and not path.endswith('.py') and not path.startswith('obench/tests/'):
            package_data.setdefault(name, []).append(path)

    def add_path(token):
        path = token.removeprefix('./').rstrip('/')
        if '/' not in path:
            # A bare top-level name ('./docs' in help text) is too coarse to
            # identify an input; single-segment literals are ignored alike.
            return
        if path in tree.files:
            if path not in files:
                files.add(path)
                if path.endswith('.py'):
                    pending.append(path)
        elif path in tree.dirs:
            directories.add(path)

    def add_module(name):
        # Importing a child also executes each package initializer. Reaching
        # a family that loads members by name includes every member.
        parts = name.split('.')
        for count in range(1, len(parts)+1):
            prefix = '.'.join(parts[:count])
            if prefix in modules:
                add_path(modules[prefix])
            family = prefix.replace('.', '/')
            if family in DYNAMIC_FAMILIES and family not in directories:
                directories.add(family)
                for member, path in modules.items():
                    if member.startswith(prefix + '.'):
                        add_path(path)

    def add_text(value, here):
        for token in PATH_TOKEN.findall(value):
            add_path(token)
        for name in MODULE_TOKEN.findall(value):
            add_module(name)
        for token in NAME_TOKEN.findall(value):
            for path in (*siblings.get((here, token), ()), *package_data.get(token, ())):
                add_path(path)

    for step, specific in lane_steps(tree.read(WORKFLOW)):
        if specific and not any(token.removeprefix('./').rstrip('/') in tree.files | tree.dirs
                                for token in PATH_TOKEN.findall(step)):
            # A lane step naming no repository path means paths no longer
            # resolve from the repository root (or the step changed shape).
            raise ValueError(f'a {LANE} step names no repository path: {step.splitlines()[0]}')
        add_text(step, '.')
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        tree_ = ast.parse(tree.read(path), filename=path)
        # Relative imports resolve against the containing package.
        package = '.'.join(PurePosixPath(path).parent.parts) if path.startswith('obench/') else ''
        for node in ast.walk(tree_):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if not package:
                        raise ValueError(f'{path}: relative import outside a package')
                    parts = package.split('.')
                    base = '.'.join(parts[:len(parts)-node.level+1])
                    module = '.'.join(filter(None, (base, node.module)))
                else:
                    module = node.module or ''
                names = [module, *(module + '.' + alias.name for alias in node.names)]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                add_text(node.value, str(PurePosixPath(path).parent))
                continue
            else:
                continue
            for name in names:
                add_module(name)
                top = name.split('.')[0]
                for directory in (str(PurePosixPath(path).parent), *SCRIPT_PATHS):
                    add_path(f'{directory}/{top}.py')
    return files, directories


def matches(path, files, directories):
    return path in files or any(path.startswith(d + '/') for d in directories)


def decide(repo, base, head):
    report = {'base': base, 'head': head}
    for label, value in (('base', base), ('head', head)):
        if not SHA.fullmatch(value or ''):
            raise ValueError(f'{label} is not a full commit SHA: {value!r}')
    if git(repo, 'rev-parse', '--is-shallow-repository').strip() != 'false':
        raise ValueError('shallow history cannot establish the merge base')
    merge_base = git(repo, 'merge-base', base, head).strip()
    report['merge_base'] = merge_base
    changed = sorted(p for p in git(repo, 'diff', '--name-only', '-z', '--no-renames',
                                    merge_base, head).split('\0') if p)
    report['changed'] = changed
    if not changed:
        raise ValueError('the comparison found no changed files')
    files, directories = set(), set()
    for rev in (merge_base, head):
        rev_files, rev_directories = inputs(Tree(repo, rev))
        files |= rev_files
        directories |= rev_directories
    report['input_files'] = len(files)
    report['input_directories'] = sorted(directories)
    report['matched'] = [p for p in changed if matches(p, files, directories)]
    report['run'] = bool(report['matched'])
    report['reason'] = ('changed lane inputs' if report['run'] else
                        'no changed file is a browser-quality input')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--repo', default='.', help='Git repository (default: current directory)')
    parser.add_argument('--base', help='Base commit SHA of the pull request')
    parser.add_argument('--head', help='Head commit SHA of the pull request')
    parser.add_argument('--list', nargs='?', const='', metavar='REV',
                        help='Print the derived input set at REV (default: the working tree) '
                             'and exit; makes no decision')
    args = parser.parse_args(argv)
    if args.list is not None:
        tree = Tree(args.repo, args.list) if args.list else WorkTree(args.repo)
        files, directories = inputs(tree)
        print(json.dumps({'files': sorted(files), 'directories': sorted(directories)}, indent=2))
        return 0
    try:
        report = decide(args.repo, args.base, args.head)
    except Exception as error:  # Fail closed: any doubt runs the lane.
        report = {'base': args.base, 'head': args.head, 'run': True,
                  'reason': f'fail-closed: {type(error).__name__}: {error}'}
    print(json.dumps(report, sort_keys=True))
    print('run=' + ('true' if report['run'] else 'false'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
