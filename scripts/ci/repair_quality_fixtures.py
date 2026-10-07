"""Public synthetic control construction; no archived model answers or private context.

The baseline/reference are already-published fixtures. The v6 addition below
implements the stated source-identity rule independently of the grader. The two
valid alternatives use different identity aggregation and diagnostic forms.
"""
from pathlib import Path
import shutil
import tomllib

from obench.sandbox_grading import task_digest

IDENTITY = '''

def _source_identity(locator):
    # Only a plugin-cache version segment is interchangeable. Preserve its
    # marketplace, package and complete relative skill filename.
    marker = '/plugins/cache/'
    if marker in locator:
        head, tail = locator.split(marker, 1)
        parts = tail.split('/')
        if len(parts) >= 5 and parts[3] == 'skills':
            return head + marker + '/'.join(parts[:2] + parts[3:])
    return locator

def qualified_identities(listing):
    return Counter(
        e.name + ':' + _source_identity(_absolute(e.locator, listing.root_lines))
        for e in listing.entries
    )
'''
ALTERNATIVE = '''

def qualified_identities(listing):
    identities = Counter()
    for entry in listing.entries:
        locator = _absolute(entry.locator, listing.root_lines)
        identities[(entry.name, _source_identity(locator))] += 1
    # Keep string diagnostics compatible with the public interface.
    return Counter({repr(identity): count for identity, count in identities.items()})
'''
COLLAPSE_SKILL = '''
_original_source_identity = _source_identity
def _source_identity(locator):
    if '/plugins/cache/' in locator:
        return _original_source_identity(locator).split('/skills/')[0]
    return locator
'''
COLLAPSE_VERSION = '''
_original_source_identity = _source_identity
def _source_identity(locator):
    return re.sub(r'/[0-9]+\\.[0-9]+\\.[0-9]+/skills/', '/skills/', _original_source_identity(locator))
'''


def prepare(root, output):
    task = output / 'task'
    shutil.copytree(root/'benchmarks/harbor/local/dojo-evidence-pr60-c3-o5', task)
    config = task/'task.toml'
    old = tomllib.loads(config.read_text())['metadata']['openbench_task_content_digest']['sha256']
    config.write_text(config.read_text().replace('oracle_revision = 5', 'oracle_revision = 6').replace('dojo-evidence-pr60-c3-o5', 'dojo-evidence-pr60-c3-o6'))
    config.write_text(config.read_text().replace(old, task_digest(task)))
    specs = [{'id': 'baseline', 'role': 'baseline', 'source': str(task/'environment/app'),
              'must_fail': ['ignore-user', 'unsupported-exec', 'removed-origin']}]
    reference = root/'benchmarks/local/dojo-evidence-pr60/solution/scripts/profiles'
    for name, extra, targets in (
        ('reference', '', []), ('alternative', ALTERNATIVE, []),
        ('budget-defect', '', ['unsupported-exec']), ('record-defect', '', ['ignore-user']),
        ('skill-defect', COLLAPSE_SKILL, ['different-skill-file']),
        ('version-defect', COLLAPSE_VERSION, ['nonplugin-version-directory']),
    ):
        source = output/name
        shutil.copytree(task/'environment/app', source)
        profiles = source/'scripts/profiles'
        for filename in ('budget.py', 'rollout_codex.py'):
            shutil.copyfile(reference/filename, profiles/filename)
        file = profiles/'rollout_codex.py'
        file.write_text(file.read_text() + IDENTITY + extra)
        if name == 'budget-defect':
            shutil.copyfile(task/'environment/app/scripts/profiles/budget.py', profiles/'budget.py')
        if name == 'record-defect':
            file.write_text(file.read_text() + '\ndef _is_harness_context(record):\n    return True\n')
        specs.append({'id': name, 'role': 'invalid' if targets else 'valid',
                      'source': str(source), 'must_fail': targets})
    return task, {'schema': 1, 'contract_review': 'Public synthetic v6 controls: developer-only evidence, surface gating, plugin-specific version equivalence and complete source identity. This is infrastructure coverage, not difficulty calibration.',
                  'project_check': 'python3 -m compileall -q scripts', 'controls': specs}
