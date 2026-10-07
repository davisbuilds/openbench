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

RUNTIME_ROOTS = ('obench.runtime_admission', 'obench.suite_run',
                 'obench.harbor_agents.sandbox_codex', 'obench.sandbox_gateway')
DYNAMIC_FAMILIES = ('obench.adapters', 'obench.harbor_agents', 'obench.repair_oracles')


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
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
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
