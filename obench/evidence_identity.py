"""Conservative local Python dependency closure for runtime qualification.

Parse imports without executing modules. Include every adapter/oracle because
those families also load by filename or registry string. Other runtime roots
name the process entry points; scripts are parsed for their local imports too.
Unknown/new ordinary imports are followed automatically. This is intentionally
coarser than call-graph analysis: unused imports can still invalidate evidence.
"""
import ast
from pathlib import Path
import re

RUNTIME_ROOTS = ('obench.__main__', 'obench.cli', 'obench.runtime_admission', 'obench.suite_run',
                 'obench.harbor_agents.sandbox_codex', 'obench.sandbox_gateway')
DYNAMIC_FAMILIES = ('obench.adapters', 'obench.harbor_agents', 'obench.repair_oracles')


def dependency_nodes(tree, *, dispatches=()):
    """Prune only literal CLI dispatches that cannot handle a repair run.

    The entire CLI source is still hashed. New/unrecognized conditions and
    unconditional imports are traversed conservatively, so changes cannot add
    an ordinary dependency without binding it.
    """
    yield tree
    if tree in dispatches and isinstance(tree, ast.If):
        test = tree.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == 'command' and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq) and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and isinstance(test.comparators[0].value, str)
                and test.comparators[0].value not in ('run', 'campaign', 'repair')):
            for node in [test, *tree.orelse]:
                yield from dependency_nodes(node, dispatches=dispatches)
            return
    for child in ast.iter_child_nodes(tree):
        yield from dependency_nodes(child, dispatches=dispatches)


def runtime_sources(root, scripts):
    root = Path(root)
    modules = {}
    for path in (root/'obench').rglob('*.py'):
        if 'tests' in path.relative_to(root).parts:
            continue
        name = '.'.join(path.relative_to(root).with_suffix('').parts).removesuffix('.__init__')
        modules[name] = path
    pending = set(RUNTIME_ROOTS)
    if not pending <= modules.keys():
        raise ValueError('runtime identity is missing a required entry point')
    pending.update(name for name in modules if any(name == family or name.startswith(family+'.') for family in DYNAMIC_FAMILIES))
    seen = set()

    def references(path, package):
        result = set()
        def include(name):
            # Importing a child executes its package initializers as well.
            parts = name.split('.')
            for count in range(1, len(parts)+1):
                prefix = '.'.join(parts[:count])
                if prefix in modules:
                    result.add(prefix)
        tree = ast.parse(path.read_text(), filename=str(path))
        dispatches = set()
        if path == root/'obench/cli.py':
            mains = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
            if len(mains) == 1:
                main = mains[0]
                assignments = [node for node in main.body if isinstance(node, ast.Assign)
                               and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                               and node.targets[0].id == 'command']
                writes = [node for node in ast.walk(main) if isinstance(node, ast.Name)
                          and node.id == 'command' and isinstance(node.ctx, (ast.Store, ast.Del))]
                expected = ast.dump(ast.parse('command = argv[0]').body[0])
                if len(writes) == 1 and len(assignments) == 1 and ast.dump(assignments[0]) == expected:
                    # Only direct branches in this one lexical scope may be
                    # command dispatch. Helpers can have unrelated parameters
                    # with the same name; their imports must remain visible.
                    dispatches = {node for node in main.body if isinstance(node, ast.If)}
        for node in dependency_nodes(tree, dispatches=dispatches):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    include(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = '.'.join(package.split('.')[:len(package.split('.'))-node.level+1])
                    name = '.'.join(filter(None, (prefix, node.module)))
                else:
                    name = node.module or ''
                include(name)
                for alias in node.names:
                    include(name+'.'+alias.name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Registry names and Python snippets passed to subprocesses.
                for name in re.findall(r'\bobench(?:\.[A-Za-z_]\w*)+', node.value):
                    include(name)
        return result

    for relative in scripts:
        pending.update(references(root/relative, ''))
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = modules[name]
        package = name if path.name == '__init__.py' else name.rpartition('.')[0]
        pending.update(references(path, package) - seen)
    return sorted(modules[name] for name in seen)
