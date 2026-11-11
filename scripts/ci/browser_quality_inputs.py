#!/usr/bin/env python3
"""Derive a Harbor lane's source inputs and decide whether its changes need CI.

Workflow steps, Python imports and literal runtime paths define each lane's
closure. Only exact lane equality and startsWith conditions can exclude steps;
unknown conditions remain applicable. Import-search directories resolve concrete
modules rather than selecting every sibling script. Runtime directory uses still
select every member, and unresolvable script search roots fail closed.

Pull requests union closures from merge base, base, head and the tested merge;
pushes compare exact before/after endpoints and union their closures. Invalid or
shallow history, empty comparisons and unrecognized source layouts fail closed.
The CLI prints JSON and a final run=true/false line, defaulting to browser-quality
for compatibility. Exceptions in the reusable API are handled by the caller.
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
LANES = ('repair-grading', 'repair-tools', 'concurrency', 'browser', 'browser-quality')
LANE = 'browser-quality'
CLASSIFIER = 'scripts/ci/browser_quality_inputs.py'
DRIVER = 'scripts/ci/harbor_ci.py'
# Families that also load by filename or registry string; mirrors
# obench.evidence_identity.DYNAMIC_FAMILIES.
DYNAMIC_FAMILIES = ('obench/adapters', 'obench/harbor_agents', 'obench/repair_oracles')
PATH_TOKEN = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+')
MODULE_TOKEN = re.compile(r'\bobench(?:\.[A-Za-z_]\w*)+')
NAME_TOKEN = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*')
SHA = re.compile(r'[0-9a-f]{40}')
LANE_EQUALITY = re.compile(r"""matrix\.lane\s*==\s*(['"])([A-Za-z0-9_-]+)\1""")
LANE_PREFIX = re.compile(r"""startsWith\(\s*matrix\.lane\s*,\s*(['"])([A-Za-z0-9_-]+)\1\s*\)""")


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


def lane_steps(text, lane=LANE):
    """Return (text, lane_specific) for each integration step the lane can run."""
    if lane not in LANES:
        raise ValueError(f'unknown Harbor lane: {lane!r}')
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
        condition = conditions[0] if len(conditions) == 1 else ''
        if condition.startswith('${{') and condition.endswith('}}'):
            condition = condition[3:-2].strip()
        match = LANE_EQUALITY.fullmatch(condition)
        prefix = LANE_PREFIX.fullmatch(condition)
        if (match and match[2] != lane) or (prefix and not lane.startswith(prefix[2])):
            continue
        specific += bool(match)
        applicable.append(('\n'.join(step), bool(match)))
    # Without a step conditioned exactly on this lane, the job layout is not
    # the one this parser understands (or predates the lane): fail closed.
    if not specific:
        raise ValueError(f"no {JOB} step is conditioned on matrix.lane == '{lane}'")
    return applicable


def modules_of(tree):
    modules = {}
    for path in tree.files:
        if path.endswith('.py') and path.startswith('obench/'):
            name = '.'.join(PurePosixPath(path).with_suffix('').parts).removesuffix('.__init__')
            modules[name] = path
    return modules


def script_search_paths(source, path, tree):
    """Resolve script sys.path mutations without treating roots as runtime data.

    Recognize only static repository paths. Anything dynamic in an entry/helper
    script raises: silently dropping its import root could produce an unsafe skip.
    Package runtime loaders retain their existing conservative path handling.
    """
    if not path.startswith('scripts/'):
        return set(), set()
    anchor = PurePosixPath('/repository')
    bindings = {'__file__': anchor / path}

    def resolve(node):
        if isinstance(node, ast.Name):
            return bindings[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return PurePosixPath(node.value)
        if isinstance(node, ast.Call) and not node.keywords:
            if isinstance(node.func, ast.Name) and node.func.id in ('Path', 'str') and len(node.args) == 1:
                return resolve(node.args[0])
            if isinstance(node.func, ast.Attribute) and node.func.attr in ('resolve', 'absolute') and not node.args:
                return resolve(node.func.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return resolve(node.left) / resolve(node.right)
        if isinstance(node, ast.Attribute) and node.attr == 'parent':
            return resolve(node.value).parent
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == 'parents' and isinstance(node.slice, ast.Constant)
                and type(node.slice.value) is int):
            return resolve(node.value.value).parents[node.slice.value]
        raise ValueError('dynamic path expression')

    stores, parameters = {}, set()
    for node in ast.walk(source):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            stores[node.id] = stores.get(node.id, 0) + 1
        elif isinstance(node, ast.arg):
            parameters.add(node.arg)

    # Roots normally use ROOT/REPO = Path(__file__).resolve().parents[2].
    for node in source.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if stores.get(target.id) != 1 or target.id in parameters:
                        bindings.pop(target.id, None)
                        continue
                    try:
                        bindings[target.id] = resolve(node.value)
                    except (KeyError, ValueError, IndexError, TypeError):
                        bindings.pop(target.id, None)

    def is_sys_path(node):
        return (isinstance(node, ast.Attribute) and node.attr == 'path'
                and isinstance(node.value, ast.Name) and node.value.id == 'sys')

    roots, ignored = set(), set()
    for node in ast.walk(source):
        values = None
        if isinstance(node, ast.Assign) and any(
                is_sys_path(t.value) if isinstance(t, ast.Subscript) else is_sys_path(t)
                for t in node.targets):
            values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and is_sys_path(node.func.value):
            if node.func.attr == 'insert' and len(node.args) == 2 and not node.keywords:
                values = [node.args[1]]
            elif node.func.attr == 'append' and len(node.args) == 1 and not node.keywords:
                values = node.args
            elif (node.func.attr == 'extend' and len(node.args) == 1 and not node.keywords
                  and isinstance(node.args[0], (ast.List, ast.Tuple))):
                values = node.args[0].elts
            else:
                raise ValueError(f'{path}: unrecognized sys.path operation')
        if isinstance(node, ast.AugAssign) and is_sys_path(node.target):
            if isinstance(node.op, ast.Add) and isinstance(node.value, (ast.List, ast.Tuple)):
                values = node.value.elts
            else:
                raise ValueError(f'{path}: unrecognized sys.path operation')
        if values is None:
            continue
        for value in values:
            try:
                root = resolve(value)
                root = root.relative_to(anchor) if root.is_absolute() else root
                if '..' in root.parts or (str(root) != '.' and str(root) not in tree.dirs):
                    raise ValueError('not a tracked repository directory')
            except (KeyError, ValueError, IndexError, TypeError) as error:
                raise ValueError(f'{path}: cannot resolve sys.path search root') from error
            roots.add(str(root))
            ignored.update(id(n) for n in ast.walk(value)
                           if isinstance(n, ast.Constant) and isinstance(n.value, str))
    return roots, ignored


def inputs(tree, lane=LANE):
    """Return (files, directories) the lane reads at this commit."""
    if lane not in LANES:
        raise ValueError(f'unknown Harbor lane: {lane!r}')
    modules = modules_of(tree)
    files, directories, pending = {WORKFLOW, CLASSIFIER, DRIVER}, set(), []
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
        if path in tree.files:
            if path not in files:
                files.add(path)
                if path.endswith('.py'):
                    pending.append(path)
        elif '/' in path and path in tree.dirs:
            # Single-segment directory literals are too coarse (e.g. './docs'
            # in help text); concrete files and resolved imports remain valid.
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

    for step, specific in lane_steps(tree.read(WORKFLOW), lane=lane):
        if specific and not any(token.removeprefix('./').rstrip('/') in tree.files | tree.dirs
                                for token in PATH_TOKEN.findall(step)) and not any(
                '.'.join(name.split('.')[:count]) in modules
                for name in MODULE_TOKEN.findall(step) for count in range(1, len(name.split('.'))+1)):
            # A lane step naming no repository path means paths no longer
            # resolve from the repository root (or the step changed shape).
            raise ValueError(f'a {lane} step names no repository path: {step.splitlines()[0]}')
        add_text(step, '.')
    # Imports in a helper inherit its entry script's sys.path. Union roots
    # conservatively across lane scripts and revisit sources when a newly read
    # script adds a root; traversal order must not drop a package dependency.
    search_roots = {'.', *(str(PurePosixPath(p).parent) for p in pending)}
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        tree_ = ast.parse(tree.read(path), filename=path)
        search_paths, search_constants = script_search_paths(tree_, path, tree)
        if not search_paths <= search_roots:
            search_roots |= search_paths
            pending.extend(seen - {path})
            seen = {path}
        # Relative imports resolve against the containing package.
        package = '.'.join(PurePosixPath(path).parent.parts) if path.startswith('obench/') else ''
        for node in ast.walk(tree_):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if not package:
                        base = PurePosixPath(path).parent
                        for _ in range(node.level):
                            if str(base / '__init__.py') not in tree.files:
                                raise ValueError(f'{path}: relative import outside a known package')
                            base = base.parent
                        base = PurePosixPath(path).parent
                        for _ in range(node.level-1):
                            base = base.parent
                        target = base.joinpath(*(node.module or '').split('.'))
                        add_path(str(target) + '.py')
                        add_path(str(target / '__init__.py'))
                        for alias in node.names:
                            child = target.joinpath(*alias.name.split('.'))
                            add_path(str(child) + '.py')
                            add_path(str(child / '__init__.py'))
                        continue
                    parts = package.split('.')
                    base = '.'.join(parts[:len(parts)-node.level+1])
                    module = '.'.join(filter(None, (base, node.module)))
                else:
                    module = node.module or ''
                names = [module, *(module + '.' + alias.name for alias in node.names)]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in search_constants:
                    add_text(node.value, str(PurePosixPath(path).parent))
                continue
            else:
                continue
            for name in names:
                add_module(name)
                for directory in (str(PurePosixPath(path).parent), *sorted(search_roots)):
                    parts = name.split('.')
                    for count in range(1, len(parts)+1):
                        candidate = str(PurePosixPath(directory).joinpath(*parts[:count]))
                        add_path(f'{candidate}.py')
                        add_path(f'{candidate}/__init__.py')
    return files, directories


def matches(path, files, directories):
    return path in files or any(path.startswith(d + '/') for d in directories)


def decide(repo, base, head, merged=None, lane=LANE, comparison='pull_request'):
    if lane not in LANES:
        raise ValueError(f'unknown Harbor lane: {lane!r}')
    if comparison not in ('pull_request', 'push'):
        raise ValueError(f'unknown comparison: {comparison!r}')
    if comparison == 'push' and merged is not None:
        raise ValueError('a push comparison cannot include a tested merge revision')
    report = {'base': base, 'head': head, 'merged': merged, 'lane': lane,
              'comparison': comparison}
    revisions = (('base', base), ('head', head), *((('merged', merged),) if merged is not None else ()))
    for label, value in revisions:
        if not SHA.fullmatch(value or ''):
            raise ValueError(f'{label} is not a full commit SHA: {value!r}')
    if git(repo, 'rev-parse', '--is-shallow-repository').strip() != 'false':
        raise ValueError('shallow history cannot establish the merge base')
    if comparison == 'pull_request':
        merge_base = git(repo, 'merge-base', base, head).strip()
        report['merge_base'] = merge_base
        revisions_to_read = (merge_base, base, head, *([merged] if merged else []))
    else:
        merge_base = base  # Exact before..after, including a force-push rollback.
        revisions_to_read = (base, head)
    changed = sorted(p for p in git(repo, 'diff', '--name-only', '-z', '--no-renames',
                                    merge_base, head).split('\0') if p)
    report['changed'] = changed
    if not changed:
        raise ValueError('the comparison found no changed files')
    files, directories = set(), set()
    for rev in dict.fromkeys(revisions_to_read):
        rev_files, rev_directories = inputs(Tree(repo, rev), lane=lane)
        files |= rev_files
        directories |= rev_directories
    report['input_files'] = len(files)
    report['input_directories'] = sorted(directories)
    report['matched'] = [p for p in changed if matches(p, files, directories)]
    report['run'] = bool(report['matched'])
    report['reason'] = ('changed lane inputs' if report['run'] else
                        f'no changed file is a {lane} input')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--lane', choices=LANES, default=LANE)
    parser.add_argument('--comparison', choices=('pull_request', 'push'), default='pull_request')
    parser.add_argument('--repo', default='.', help='Git repository (default: current directory)')
    parser.add_argument('--base', help='Base commit SHA of the pull request')
    parser.add_argument('--head', help='Head commit SHA of the pull request')
    parser.add_argument('--merged', help='Commit SHA of the merge revision under test (optional)')
    parser.add_argument('--list', nargs='?', const='', metavar='REV',
                        help='Print the derived input set at REV (default: the working tree) '
                             'and exit; makes no decision')
    args = parser.parse_args(argv)
    if args.list is not None:
        tree = Tree(args.repo, args.list) if args.list else WorkTree(args.repo)
        files, directories = inputs(tree, lane=args.lane)
        print(json.dumps({'files': sorted(files), 'directories': sorted(directories)}, indent=2))
        return 0
    try:
        report = decide(args.repo, args.base, args.head, args.merged,
                        lane=args.lane, comparison=args.comparison)
    except Exception as error:  # Fail closed: any doubt runs the lane.
        report = {'base': args.base, 'head': args.head, 'merged': args.merged, 'run': True,
                  'reason': f'fail-closed: {type(error).__name__}: {error}'}
    print(json.dumps(report, sort_keys=True))
    print('run=' + ('true' if report['run'] else 'false'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
