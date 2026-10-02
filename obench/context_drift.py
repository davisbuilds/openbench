"""Read-only comparison of frozen skills with explicit current source roots.

This is a freshness diagnostic, not archive validation or a synchronization tool.
It never changes a treatment or follows source locators supplied by provenance.
"""
import hashlib
from pathlib import Path
import re

from .frozen_context import MAX_BYTES, MAX_FILES, _regular

# Match capture's disposable-file exclusions; report all other support files.
IGNORED = frozenset(('.git', '__pycache__', '.DS_Store'))


def _tree(path):
    # Installed skills commonly link to a canonical global directory. Resolve
    # only the explicitly selected skill root; reject links inside its tree.
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('skill root is not a directory')
    files, size = {}, 0
    pending = [root]
    while pending:
        directory = pending.pop()
        for p in sorted(directory.iterdir()):
            if p.name in IGNORED:
                continue
            if p.is_symlink():
                raise ValueError('nested source symlink is not auditable')
            if p.is_dir():
                pending.append(p)
            else:
                data, mode = _regular(p)
                size += len(data)
                if len(files) >= MAX_FILES or size > MAX_BYTES // 2:
                    raise ValueError('source skill exceeds audit bounds')
                files[p.relative_to(root).as_posix()] = {
                    'sha256': hashlib.sha256(data).hexdigest(), 'mode': mode}
    return files


def _baseline(archive, provenance):
    files = {n: {'sha256': info['sha256'], 'mode': info['mode']}
             for n, info in archive.manifest['files'].items() if n.startswith('codex/skills/')}
    skills = sorted(n.split('/')[2] for n in files if len(n.split('/')) == 4 and n.endswith('/SKILL.md'))
    if not skills or any(n.split('/')[2] not in skills for n in files):
        raise ValueError('archive must contain complete selected skills with SKILL.md')
    adapted = []
    if provenance is not None:
        if not isinstance(provenance, dict) or not isinstance(provenance.get('files'), list):
            raise ValueError('provenance requires a files list')
        seen = set()
        for item in provenance['files']:
            if not isinstance(item, dict) or not isinstance(item.get('destination'), str):
                raise ValueError('invalid provenance file record')
            name = item['destination']
            if not name.startswith('codex/skills/'):
                continue
            if name in seen or name not in files:
                raise ValueError('duplicate or unexpected skill provenance')
            seen.add(name)
            original = item.get('original_sha256')
            mode = item.get('original_mode', files[name]['mode'])
            if (item.get('staged_sha256') != files[name]['sha256']
                    or not isinstance(original, str) or not re.fullmatch('[a-f0-9]{64}', original)
                    or type(mode) is not int or mode not in (0o644, 0o755)):
                raise ValueError('provenance does not match frozen skill files')
            if original != files[name]['sha256'] or mode != files[name]['mode']:
                adapted.append(name)
            files[name] = {'sha256': original, 'mode': mode}
        if seen != set(files):
            raise ValueError('provenance must cover every frozen skill file')
    return skills, files, sorted(adapted)


def audit_skills(archive, roots, provenance=None):
    """Compare full selected trees; missing/unreadable inputs are incomplete.

    Roots map operator-defined labels to paths containing skill directories.
    Optional reviewed provenance supplies pre-adaptation hashes; without it the
    frozen bytes themselves are the baseline. Unselected skills are out of scope.
    """
    if archive.manifest['kind'] != 'context' or not roots:
        raise ValueError('skill audit requires a context archive and source roots')
    skills, baseline, adapted = _baseline(archive, provenance)
    sources, incomplete, drift = [], False, False
    for label, root in roots.items():
        source = {'label': label, 'root': str(Path(root).expanduser().absolute()), 'skills': {}}
        for skill in skills:
            prefix = 'codex/skills/' + skill + '/'
            expected = {n[len(prefix):]: info for n, info in baseline.items() if n.startswith(prefix)}
            try:
                current = _tree(Path(source['root']) / skill)
            except (OSError, ValueError, RuntimeError) as exc:
                source['skills'][skill] = {'status': 'unavailable', 'error': str(exc)}
                incomplete = True
                continue
            changes = []
            for name in sorted(set(expected) | set(current)):
                if name not in expected:
                    changes.append({'path': name, 'kind': 'added'})
                elif name not in current:
                    changes.append({'path': name, 'kind': 'removed'})
                else:
                    if current[name]['sha256'] != expected[name]['sha256']:
                        changes.append({'path': name, 'kind': 'content'})
                    if current[name]['mode'] != expected[name]['mode']:
                        changes.append({'path': name, 'kind': 'mode'})
            source['skills'][skill] = {'status': 'drift' if changes else 'clean', 'changes': changes}
            drift |= bool(changes)
        sources.append(source)
    return {'schema': 1, 'archive_sha256': archive.sha256,
            'status': 'incomplete' if incomplete else 'drift' if drift else 'clean',
            'baseline': 'original-provenance' if provenance is not None else 'frozen-bytes',
            'skills': skills, 'adapted_files': adapted, 'sources': sources}


def parse_roots(values):
    roots = {}
    for value in values:
        label, separator, path = value.partition('=')
        if not separator or not path or not re.fullmatch('[A-Za-z0-9_-]+', label) or label in roots:
            raise ValueError('--skills-root requires unique LABEL=PATH entries')
        roots[label] = Path(path)
    return roots
