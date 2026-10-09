"""Bounded, deterministic archives. No extraction of archive-controlled paths.

Context is configuration *content*, never credentials or executable harness
configuration. Checkout exports read pinned Git objects, not a working tree.
"""
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile

MAX_BYTES = 64 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_FILES = 8192


@dataclass(frozen=True)
class Archive:
    sha256: str
    manifest: dict
    files: dict[str, bytes]


def _name(name):
    if (not isinstance(name, str) or not name or '\\' in name
            or any(ord(c) < 32 for c in name) or PurePosixPath(name).is_absolute()
            or any(p in ('', '.', '..', '.git') for p in name.split('/'))):
        raise ValueError('unsafe archive path')
    return name


def _context_name(name):
    _name(name)
    if name == 'codex/AGENTS.md':
        return
    if (name.startswith('codex/skills/') and len(name.split('/')) >= 4
            and name.split('/')[2] != '.system') or name.startswith('resources/'):
        if any(p in ('auth.json', 'config.toml', '.env', 'sessions') for p in name.split('/')):
            raise ValueError('context cannot contain auth, harness configuration or sessions')
        return
    raise ValueError('context supports codex/AGENTS.md, codex/skills and resources only')


def _regular(path, limit=MAX_FILE):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('input cannot contain symlinks')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_nlink != 1:
            raise ValueError('input must be a bounded regular file')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('input exceeds limit')
    return data, 0o755 if info.st_mode & 0o111 else 0o644


def _write(files, modes, output, kind, provenance):
    if not files or len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_BYTES // 2:
        raise ValueError('archive payload exceeds bounds or is empty')
    manifest = {'schema': 1, 'kind': kind, 'provenance': provenance, 'files': {
        n: {'sha256': hashlib.sha256(b).hexdigest(), 'size': len(b), 'mode': modes[n]}
        for n, b in sorted(files.items())}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.USTAR_FORMAT) as tar:
        entries = {'manifest.json': json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()}
        entries.update({'payload/' + n: b for n, b in files.items()})
        for name, data in sorted(entries.items()):
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    if len(raw) > MAX_BYTES:
        raise ValueError('archive exceeds limit')
    # Exclusive creation: historical artifacts are never overwritten.
    with Path(output).open('xb') as stream:
        os.chmod(output, 0o600)
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


def freeze_context(root, output):
    root = Path(root).absolute()
    if not root.is_dir() or root.is_symlink():
        raise ValueError('context root must be a directory')
    files, modes = {}, {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise ValueError('context cannot contain symlinks')
        if p.is_dir():
            continue
        name = p.relative_to(root).as_posix(); _context_name(name)
        files[name], modes[name] = _regular(p)
    if 'codex/AGENTS.md' not in files:
        raise ValueError('context requires explicit global guidance')
    return _write(files, modes, output, 'context', {})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate manifest key')
        result[key] = value
    return result


def load_archive(path, sha256, *, kind):
    if kind not in ('context', 'checkout', 'evaluation') or not isinstance(sha256, str) or not re.fullmatch('[a-f0-9]{64}', sha256):
        raise ValueError('archive requires a kind and SHA256 pin')
    raw, _ = _regular(path, MAX_BYTES)
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('archive digest mismatch')
    entries = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as tar:
            for member in tar:
                _name(member.name)
                if (not member.isfile() or member.name in entries or member.size > MAX_FILE
                        or member.size < 0 or len(entries) >= MAX_FILES + 1):
                    raise ValueError('invalid archive member')
                entries[member.name] = tar.extractfile(member).read()
        manifest = json.loads(entries.pop('manifest.json'), object_pairs_hook=_unique)
        if (set(manifest) != {'schema', 'kind', 'files', 'provenance'}
                or type(manifest['schema']) is not int or manifest['schema'] != 1
                or manifest['kind'] != kind or not isinstance(manifest['files'], dict)
                or not isinstance(manifest['provenance'], dict) or not manifest['files']):
            raise ValueError('invalid archive manifest')
        files = {}
        for name, info in manifest['files'].items():
            (_context_name if kind == 'context' else _name)(name)
            data = entries.pop('payload/' + name)
            if (set(info) != {'sha256', 'size', 'mode'}
                    or type(info['mode']) is not int or info['mode'] not in (0o644, 0o755)
                    or type(info['size']) is not int or info['size'] != len(data)
                    or info['sha256'] != hashlib.sha256(data).hexdigest()):
                raise ValueError('archive file mismatch')
            files[name] = data
        if entries or sum(map(len, files.values())) > MAX_BYTES // 2:
            raise ValueError('unexpected archive payload')
        if kind == 'context' and 'codex/AGENTS.md' not in files:
            raise ValueError('context requires global guidance')
        # Detect file/directory collisions before staging any content.
        for name in files:
            if any(str(parent) in files for parent in PurePosixPath(name).parents):
                raise ValueError('archive path collision')
        return Archive(sha256, manifest, files)
    except (tarfile.TarError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid frozen archive') from exc


def freeze_checkout(repository, commit, output, *, exclusions=None):
    if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', commit):
        raise ValueError('checkout requires a full commit ID')
    exclusions = {} if exclusions is None else exclusions
    if not isinstance(exclusions, dict):
        raise ValueError('exclusions must map paths to reasons')
    for path, reason in exclusions.items():
        _name(path)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('every exclusion needs a reason')
    # ls-tree and cat-file do not execute checkout filters or project code.
    base = ['git', '--no-replace-objects', '-C', str(repository)]
    actual = subprocess.check_output([*base, 'rev-parse', commit + '^{commit}'], text=True).strip()
    if actual != commit:
        raise ValueError('revision is not the pinned commit')
    tree = subprocess.check_output([*base, 'ls-tree', '-rz', commit])
    files, modes, matched = {}, {}, set()
    for item in tree.split(b'\0'):
        if not item:
            continue
        fields, raw_name = item.split(b'\t', 1)
        mode, typ, oid = fields.decode().split()
        name = raw_name.decode(); _name(name)
        omitted = [p for p in exclusions if name == p or name.startswith(p + '/')]
        if omitted:
            matched.update(omitted); continue
        if mode not in ('100644', '100755') or typ != 'blob':
            raise ValueError('checkout links/submodules require explicit reviewed exclusions')
        size = int(subprocess.check_output([*base, 'cat-file', '-s', oid]))
        if size > MAX_FILE or len(files) >= MAX_FILES or sum(map(len, files.values())) + size > MAX_BYTES // 2:
            raise ValueError('checkout exceeds bounds')
        files[name] = subprocess.check_output([*base, 'cat-file', 'blob', oid])
        modes[name] = 0o755 if mode == '100755' else 0o644
    if matched != set(exclusions):
        raise ValueError('exclusion did not match pinned checkout')
    return _write(files, modes, output, 'checkout', {'commit': commit, 'exclusions': exclusions})


def materialize(archive, destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination must be fresh')
    if any(p.is_symlink() for p in destination.parents):
        raise ValueError('destination cannot traverse symlinks')
    with tempfile.TemporaryDirectory(prefix='.frozen-', dir=destination.parent) as tmp:
        staged = Path(tmp) / 'files'; staged.mkdir()
        for name, data in archive.files.items():
            _name(name)
            p = staged / name; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data); p.chmod(archive.manifest['files'][name]['mode'])
        os.rename(staged, destination)


def verify_instruction_context(messages, archive):
    """Check initial AGENTS injection and the pinned CLI's generated catalog.

    Paths quoted in user instructions, tool output or later echoes are not
    discovery evidence. Catalog roots must resolve to the staged skills.
    """
    guidance, catalogs = [], []
    for message in messages:
        if message.get('role') == 'assistant' or message.get('type') in ('function_call', 'custom_tool_call'):
            break
        if message.get('type') != 'message':
            continue
        for item in message.get('content', []):
            text = item.get('text') if isinstance(item, dict) else None
            if not isinstance(text, str):
                continue
            if message.get('role') == 'user' and text.startswith('# AGENTS.md instructions'):
                guidance.append(text)
            if (message.get('role') == 'developer' and text.startswith('<skills_instructions>\n')
                    and text.rstrip().endswith('</skills_instructions>')):
                catalogs.append(text)
    if archive.files['codex/AGENTS.md'].decode().strip() not in '\n'.join(guidance):
        raise ValueError('captured global guidance missing from actual session')
    skills = sorted(n.split('/')[2] for n in archive.files
                    if n.startswith('codex/skills/') and n.endswith('/SKILL.md'))
    discovered = set()
    for catalog in catalogs:
        roots = dict(re.findall(r'^- `(r\d+)` = `([^`]+)`$', catalog, re.M))
        _, separator, entries = catalog.partition('### Available skills\n')
        if not separator:
            continue
        for path in re.findall(r'^- .+ \(file: ([^)\n]+)\)$', entries, re.M):
            alias, _, relative = path.partition('/')
            resolved = roots[alias] + '/' + relative if alias in roots else path
            discovered.add(resolved)
    if any('/tmp/codex-home/skills/' + name + '/SKILL.md' not in discovered for name in skills):
        raise ValueError('captured skills missing from actual session catalog')
    return {'sha256': archive.sha256, 'global_loaded': True, 'skills_discovered': skills}


def verify_session_context(sessions, archive):
    """Use original instruction messages from one raw session, not agent claims."""
    paths = list(Path(sessions).rglob('*.jsonl'))
    if len(paths) != 1:
        raise ValueError('context verification requires one raw Codex session')
    events = [json.loads(line) for line in paths[0].read_text().splitlines()]
    return verify_instruction_context([event.get('payload', {}) for event in events
                                       if event.get('type') == 'response_item'], archive)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    context = sub.add_parser('context', help='freeze an explicitly prepared context directory')
    context.add_argument('source', type=Path); context.add_argument('--output', type=Path, required=True)
    checkout = sub.add_parser('checkout', help='freeze a full Git commit with reviewed exclusions')
    checkout.add_argument('repository', type=Path); checkout.add_argument('--commit', required=True)
    checkout.add_argument('--exclusions', type=Path, help='JSON object mapping exact paths/prefixes to reasons')
    checkout.add_argument('--output', type=Path, required=True)
    verify = sub.add_parser('verify', help='verify an archive without extracting it')
    verify.add_argument('archive', type=Path); verify.add_argument('--sha256', required=True)
    verify.add_argument('--kind', choices=('context', 'checkout'), required=True)
    unpack = sub.add_parser('unpack', help='verify and populate a new directory')
    unpack.add_argument('archive', type=Path); unpack.add_argument('--sha256', required=True)
    unpack.add_argument('--kind', choices=('context', 'checkout'), required=True)
    unpack.add_argument('--destination', type=Path, required=True)
    audit = sub.add_parser('audit-skills', help='report frozen skill drift against explicit current source roots')
    audit.add_argument('archive', type=Path)
    audit.add_argument('--sha256', required=True)
    audit.add_argument('--skills-root', action='append', required=True, metavar='LABEL=PATH',
                       help='directory containing current skill trees; repeat for canonical and installed roots')
    audit.add_argument('--provenance', type=Path,
                       help='private capture records with original/staged hashes for intentional adaptations')
    args = parser.parse_args(argv)
    try:
        if args.command == 'audit-skills':
            import sys
            from .context_drift import audit_skills, parse_roots
            roots = parse_roots(args.skills_root)
            loaded = load_archive(args.archive, args.sha256, kind='context')
            provenance = json.loads(_regular(args.provenance)[0], object_pairs_hook=_unique) if args.provenance else None
            report = audit_skills(loaded, roots, provenance)
            print(json.dumps(report, sort_keys=True))
            if report['status'] != 'clean':
                print('WARNING: frozen skill audit is ' + report['status'] +
                      '; inspect per-source changes before selecting the next treatment. Archive unchanged.', file=sys.stderr)
            return {'clean': 0, 'drift': 1, 'incomplete': 2}[report['status']]
        elif args.command == 'context':
            digest = freeze_context(args.source, args.output)
        elif args.command == 'checkout':
            exclusions = json.loads(args.exclusions.read_text(), object_pairs_hook=_unique) if args.exclusions else {}
            digest = freeze_checkout(args.repository, args.commit, args.output, exclusions=exclusions)
        else:
            loaded = load_archive(args.archive, args.sha256, kind=args.kind)
            if args.command == 'unpack':
                materialize(loaded, args.destination)
            digest = loaded.sha256
        print(json.dumps({'schema': 1, 'sha256': digest}))
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(2, 'frozen archive: ' + str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
