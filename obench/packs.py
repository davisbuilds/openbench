#!/usr/bin/env python3
"""Versioned OpenBench task packs (``org/name@version``).

Packs are directories of tasks plus a ``pack.toml``. They install under
``.openbench/packs/<org>/<name>/<version>/`` from local directories, git
repositories, or plain HTTPS zip/tarball URLs — no custom package server and
no new dependencies (stdlib + git CLI for git sources).

    obench pack init
    obench pack install org/name@version --from <source>
    obench pack list
    obench pack verify [org/name@version]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile

from .admission_gate import Finding, structure_findings
from .publish import DIGEST_SCHEME_CURRENT, task_content_digest
from .workspace import WorkspaceError, _git_archive_extract, _run_git, resolve_commit_sha

PACK_TOML = "pack.toml"
PACK_SOURCE_JSON = "pack_source.json"
DEFAULT_PACKS_DIRNAME = os.path.join(".openbench", "packs")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
_SPEC_RE = re.compile(
    r"^(?P<org>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:@(?P<version>.+))?$"
)

# Soft structure rules for pack install: polarity/oracle extras warn only.
_STRUCTURE_WARN_RULES = frozenset({"structure.missing"})
_STRUCTURE_WARN_PATHS = frozenset({"solution", "PROVENANCE.md"})


class PackError(ValueError):
    """User-facing pack failure."""


# ---------------------------------------------------------------------------
# Identity / pack.toml
# ---------------------------------------------------------------------------


def parse_pack_spec(spec: str) -> dict:
    """Parse ``org/name`` or ``org/name@version`` into a dict."""
    if not isinstance(spec, str) or not spec.strip():
        raise PackError("pack spec must be a non-empty string (org/name@version)")
    match = _SPEC_RE.fullmatch(spec.strip())
    if not match:
        raise PackError(
            f"invalid pack spec {spec!r}; expected org/name or org/name@version"
        )
    org = match.group("org")
    name = match.group("name")
    version = match.group("version")
    if version is not None:
        version = version.strip()
        if not version or not _VERSION_RE.fullmatch(version):
            raise PackError(
                f"invalid pack version {version!r}; expected semver "
                "(e.g. 1.0.0 or 1.0.0-beta.1)"
            )
    return {"org": org, "name": name, "version": version}


def _require_id(value, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PackError(
            f"{field} must match [A-Za-z0-9][A-Za-z0-9._-]*; got {value!r}"
        )
    return value


def _require_version(value, field: str = "version") -> str:
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value.strip()):
        raise PackError(
            f"{field} must be semver (e.g. 1.0.0); got {value!r}"
        )
    return value.strip()


def load_pack_toml(path: str) -> dict:
    """Parse and validate a ``pack.toml``.

    Required: ``org``, ``name``, ``version``. Optional: ``description``,
    ``license``, ``tasks`` (explicit list; omit for auto-discovery).
    """
    path = os.path.abspath(path)
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise PackError(f"missing pack.toml: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PackError(f"invalid pack.toml: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PackError(f"pack.toml must be a table: {path}")

    org = _require_id(raw.get("org"), "org")
    name = _require_id(raw.get("name"), "name")
    version = _require_version(raw.get("version"), "version")

    description = raw.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise PackError(f"{path}: description must be a string")

    license_ = raw.get("license", "")
    if license_ is None:
        license_ = ""
    if not isinstance(license_, str):
        raise PackError(f"{path}: license must be a string")

    tasks = raw.get("tasks")
    if tasks is not None:
        if not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks):
            raise PackError(f"{path}: tasks must be a list of strings when set")
        tasks = [t.strip() for t in tasks]
        if not tasks or any(not t for t in tasks):
            raise PackError(f"{path}: tasks list must be non-empty when set")
        for t in tasks:
            if "/" in t or t in (".", "..") or not _ID_RE.fullmatch(t):
                raise PackError(f"{path}: invalid task name {t!r}")

    return {
        "org": org,
        "name": name,
        "version": version,
        "description": description.strip(),
        "license": license_.strip(),
        "tasks": tasks,
        "path": path,
    }


def render_pack_toml(
    *,
    org: str,
    name: str,
    version: str,
    description: str = "",
    license_: str = "Apache-2.0",
    tasks: list[str] | None = None,
) -> str:
    """Render a ``pack.toml`` scaffold (stdlib — no toml writer dependency)."""
    org = _require_id(org, "org")
    name = _require_id(name, "name")
    version = _require_version(version)
    lines = [
        f'org = "{org}"',
        f'name = "{name}"',
        f'version = "{version}"',
        f'description = "{_escape_toml_str(description)}"',
        f'license = "{_escape_toml_str(license_)}"',
        "",
    ]
    if tasks is not None:
        rendered = ", ".join(f'"{_escape_toml_str(t)}"' for t in tasks)
        lines.append(f"tasks = [{rendered}]")
        lines.append("")
    else:
        lines.append("# tasks = [\"task-a\", \"task-b\"]  # omit to auto-discover")
        lines.append("")
    return "\n".join(lines)


def _escape_toml_str(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def discover_pack_tasks(pack_dir: str, meta: dict | None = None) -> list[str]:
    """Return task directory names for a pack (explicit list or auto-discover)."""
    pack_dir = os.path.abspath(pack_dir)
    meta = meta or load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    if meta.get("tasks") is not None:
        names = list(meta["tasks"])
        missing = [
            n for n in names
            if not os.path.isdir(os.path.join(pack_dir, n))
        ]
        if missing:
            raise PackError(
                f"pack.toml tasks not found under {pack_dir}: {', '.join(missing)}"
            )
        return names

    names = []
    for entry in sorted(os.listdir(pack_dir)):
        if entry.startswith(".") or entry in (PACK_TOML, PACK_SOURCE_JSON):
            continue
        path = os.path.join(pack_dir, entry)
        if not os.path.isdir(path):
            continue
        # A task root has instruction.md or checker.sh (same heuristic as validate).
        if os.path.isfile(os.path.join(path, "instruction.md")) or os.path.isfile(
            os.path.join(path, "checker.sh")
        ):
            names.append(entry)
    if not names:
        raise PackError(f"no tasks found under pack directory: {pack_dir}")
    return names


def pack_identity(meta: dict) -> str:
    return f"{meta['org']}/{meta['name']}@{meta['version']}"


def resolve_install_identity(spec: str, meta: dict) -> dict:
    """Merge CLI spec with pack.toml; fail on org/name/version mismatches."""
    parsed = parse_pack_spec(spec)
    if parsed["org"] != meta["org"] or parsed["name"] != meta["name"]:
        raise PackError(
            f"spec {spec!r} does not match pack.toml identity "
            f"{meta['org']}/{meta['name']}"
        )
    if parsed["version"] is None:
        version = meta["version"]
    elif parsed["version"] != meta["version"]:
        raise PackError(
            f"spec version {parsed['version']!r} does not match "
            f"pack.toml version {meta['version']!r}"
        )
    else:
        version = parsed["version"]
    return {"org": meta["org"], "name": meta["name"], "version": version}


# ---------------------------------------------------------------------------
# Layout / provenance
# ---------------------------------------------------------------------------


def default_packs_root(start: str | None = None) -> str:
    """``.openbench/packs`` under ``start`` (default: cwd)."""
    base = os.path.abspath(start or os.getcwd())
    return os.path.join(base, DEFAULT_PACKS_DIRNAME)


def pack_install_dir(packs_root: str, org: str, name: str, version: str) -> str:
    return os.path.join(
        os.path.abspath(packs_root),
        _require_id(org, "org"),
        _require_id(name, "name"),
        _require_version(version),
    )


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_tree(root: str) -> str:
    """Stable content digest of a directory tree (paths relative to root)."""
    root = os.path.abspath(root)
    hasher = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\0")
            with open(full, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
            hasher.update(b"\0")
    return hasher.hexdigest()


def write_pack_source(dest_dir: str, provenance: dict) -> str:
    path = os.path.join(dest_dir, PACK_SOURCE_JSON)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(provenance, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def load_pack_source(pack_dir: str) -> dict | None:
    path = os.path.join(pack_dir, PACK_SOURCE_JSON)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise PackError(f"invalid {PACK_SOURCE_JSON}: {path}")
    return data


# ---------------------------------------------------------------------------
# Source materialization
# ---------------------------------------------------------------------------


def _looks_like_url(source: str) -> bool:
    return source.startswith(("https://", "http://", "git+", "git://"))


def _looks_like_archive_url(source: str) -> bool:
    lower = source.lower().split("?", 1)[0]
    return lower.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2"))


def classify_source(source: str) -> str:
    """Return ``dir``, ``git``, or ``https`` for a ``--from`` value."""
    source = source.strip()
    if not source:
        raise PackError("--from source must be non-empty")
    if source.startswith("git+") or source.startswith("git://"):
        return "git"
    if source.startswith(("https://", "http://")):
        if source.endswith(".git") or ".git@" in source or ".git#" in source:
            return "git"
        if _looks_like_archive_url(source):
            return "https"
        # Bare https repo URLs without .git still treat as git when path has no archive suffix.
        return "git"
    if os.path.isdir(source):
        return "dir"
    raise PackError(
        f"unsupported --from source {source!r}; use a local directory, "
        "git URL (git+https://… or https://…/.git), or https archive URL "
        "(.zip / .tar.gz)"
    )


def _parse_git_source(source: str) -> tuple[str, str | None, str | None]:
    """Return ``(url_or_path, ref, subdir)`` from a git ``--from`` value.

    Accepts:
      git+https://host/repo.git[@ref][#subdir]
      https://host/repo.git[@ref][#subdir]
      /local/repo[#subdir]  (ref via --git-ref)
    """
    raw = source.strip()
    if raw.startswith("git+"):
        raw = raw[4:]
    subdir = None
    if "#" in raw:
        raw, subdir = raw.rsplit("#", 1)
        subdir = subdir.strip() or None
    ref = None
    # Split @ref only after scheme://host… — keep user@host alone.
    if raw.startswith(("http://", "https://", "git://", "ssh://")):
        # Prefer …\.git@ref; else last @ if it looks like a ref not a userinfo.
        git_at = re.search(r"\.git@([^/@]+)$", raw)
        if git_at:
            ref = git_at.group(1)
            raw = raw[: git_at.start()] + ".git"
        elif "@" in raw:
            # https://user:pass@host/repo@ref — rare; only split trailing @ref
            # when path contains .git already handled. Leave as-is.
            pass
    elif "@" in raw and not os.path.isdir(raw.split("@", 1)[0]):
        # local-looking path with @ref
        base, maybe_ref = raw.rsplit("@", 1)
        if os.path.isdir(base) or _looks_like_url(base):
            raw, ref = base, maybe_ref
    return raw, ref, subdir


def materialize_source(
    source: str,
    dest: str,
    *,
    git_ref: str | None = None,
    git_subdir: str | None = None,
) -> dict:
    """Copy/fetch ``source`` into empty ``dest``; return provenance dict.

    A local directory is copied as ``kind=dir`` unless ``--git-ref`` /
    ``--git-subdir`` is set (or the source is a ``git+…`` URL), in which case
    the tree is exported via ``git archive`` for SHA provenance.
    """
    kind = classify_source(source)
    if kind == "dir" and (git_ref or git_subdir):
        kind = "git"
    if kind == "dir":
        return _materialize_dir(source, dest)
    if kind == "git":
        return _materialize_git(
            source, dest, git_ref=git_ref, git_subdir=git_subdir
        )
    if kind == "https":
        return _materialize_https(source, dest)
    raise PackError(f"unsupported source kind: {kind}")


def _materialize_dir(source: str, dest: str) -> dict:
    src = os.path.abspath(source)
    if not os.path.isdir(src):
        raise PackError(f"local pack source is not a directory: {src}")
    _copy_pack_tree(src, dest)
    return {
        "kind": "dir",
        "path": src,
        "content_sha256": _sha256_tree(dest),
    }


def _materialize_git(
    source: str,
    dest: str,
    *,
    git_ref: str | None = None,
    git_subdir: str | None = None,
) -> dict:
    url, ref_from_url, subdir_from_url = _parse_git_source(source)
    ref = git_ref or ref_from_url or "HEAD"
    subdir = git_subdir if git_subdir is not None else subdir_from_url

    tmp_repo = None
    try:
        if os.path.isdir(url):
            repo_path = os.path.abspath(url)
            resolved = resolve_commit_sha(repo_path, ref)
            _git_archive_extract(repo_path, resolved, dest, subdir)
            provenance = {
                "kind": "git",
                "repo": repo_path,
                "ref": ref,
                "resolved_sha": resolved,
                "subdir": subdir,
                "content_sha256": _sha256_tree(dest),
            }
            return provenance

        tmp_repo = tempfile.mkdtemp(prefix="obench-pack-git-")
        _clone_for_pack(url, tmp_repo, ref)
        try:
            resolved = resolve_commit_sha(tmp_repo, ref)
        except WorkspaceError:
            resolved = resolve_commit_sha(tmp_repo, "FETCH_HEAD")
        _git_archive_extract(tmp_repo, resolved, dest, subdir)
        return {
            "kind": "git",
            "repo": url,
            "ref": ref,
            "resolved_sha": resolved,
            "subdir": subdir,
            "content_sha256": _sha256_tree(dest),
        }
    except WorkspaceError as exc:
        raise PackError(f"git pack source failed: {exc}") from exc
    finally:
        if tmp_repo is not None:
            shutil.rmtree(tmp_repo, ignore_errors=True)


def _clone_for_pack(url: str, dest: str, ref: str) -> None:
    """Shallow-ish clone/fetch of ``ref`` into ``dest`` (empty dir)."""
    _run_git(["git", "init", "--quiet", dest])
    _run_git(["git", "-C", dest, "remote", "add", "origin", url])
    fetch = ["git", "-C", dest, "fetch", "--quiet", "--depth", "1", "origin", ref]
    try:
        _run_git(fetch)
    except WorkspaceError:
        # Fall back to full fetch of ref (needed for arbitrary SHAs on some hosts).
        _run_git(["git", "-C", dest, "fetch", "--quiet", "origin", ref])


def _materialize_https(source: str, dest: str) -> dict:
    url = source.strip()
    tmp = tempfile.mkdtemp(prefix="obench-pack-http-")
    try:
        archive_path = os.path.join(tmp, "archive")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "obench-pack/1"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(archive_path, "wb") as fh:
                    shutil.copyfileobj(resp, fh)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PackError(f"failed to download pack archive: {url}: {exc}") from exc

        archive_sha = _sha256_file(archive_path)
        extract_root = os.path.join(tmp, "extract")
        os.makedirs(extract_root, exist_ok=True)
        _extract_archive(archive_path, extract_root, url)
        pack_root = _find_pack_root(extract_root)
        _copy_pack_tree(pack_root, dest)
        return {
            "kind": "https",
            "url": url,
            "archive_sha256": archive_sha,
            "content_sha256": _sha256_tree(dest),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_archive(archive_path: str, dest: str, url: str) -> None:
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest)
        except zipfile.BadZipFile as exc:
            raise PackError(f"invalid zip archive: {url}") from exc
        return
    try:
        with tarfile.open(archive_path) as tf:
            # filter="data" is 3.12+; fall back without it on older runtimes.
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                tf.extractall(dest)
    except tarfile.TarError as exc:
        raise PackError(f"invalid tar archive: {url}") from exc


def _find_pack_root(extract_root: str) -> str:
    """If the archive has a single top-level dir with pack.toml, use it."""
    direct = os.path.join(extract_root, PACK_TOML)
    if os.path.isfile(direct):
        return extract_root
    entries = [
        e for e in os.listdir(extract_root)
        if not e.startswith(".")
    ]
    if len(entries) == 1:
        nested = os.path.join(extract_root, entries[0])
        if os.path.isdir(nested) and os.path.isfile(os.path.join(nested, PACK_TOML)):
            return nested
    # Search one level deep.
    for dirpath, dirnames, filenames in os.walk(extract_root):
        if PACK_TOML in filenames:
            return dirpath
        # Keep walk shallow-ish: only top two levels.
        rel = os.path.relpath(dirpath, extract_root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth >= 2:
            dirnames.clear()
    raise PackError(
        f"downloaded archive has no {PACK_TOML} (searched under extract root)"
    )


def _copy_pack_tree(src: str, dest: str) -> None:
    """Copy pack contents into ``dest`` (must exist and be empty-ish)."""
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(src):
        if name in (PACK_SOURCE_JSON, ".git"):
            continue
        s = os.path.join(src, name)
        d = os.path.join(dest, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, symlinks=False, dirs_exist_ok=False)
        elif os.path.isfile(s):
            shutil.copy2(s, d)


# ---------------------------------------------------------------------------
# Install validation
# ---------------------------------------------------------------------------


def validate_pack_tasks(pack_dir: str, meta: dict | None = None) -> list[Finding]:
    """Structural checks for every task in the pack.

    Hard failures: missing instruction/checker/workspace, workspace conflicts.
    Warnings: missing ``solution/`` or ``PROVENANCE.md`` (polarity/oracle extras).
    """
    pack_dir = os.path.abspath(pack_dir)
    meta = meta or load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    findings: list[Finding] = []
    for task_name in discover_pack_tasks(pack_dir, meta):
        task_dir = os.path.join(pack_dir, task_name)
        for finding in structure_findings(task_dir):
            if (
                finding.rule in _STRUCTURE_WARN_RULES
                and finding.path in _STRUCTURE_WARN_PATHS
            ):
                findings.append(Finding(
                    finding.rule,
                    "warn",
                    f"{task_name}: {finding.message} "
                    "(polarity / admission may require it)",
                    path=f"{task_name}/{finding.path}" if finding.path else task_name,
                    detail=finding.detail,
                ))
            else:
                findings.append(Finding(
                    finding.rule,
                    finding.level,
                    f"{task_name}: {finding.message}",
                    path=f"{task_name}/{finding.path}" if finding.path else task_name,
                    detail=finding.detail,
                ))
    return findings


def _print_findings(findings: list[Finding]) -> tuple[int, int]:
    hard = warn = 0
    for f in findings:
        if f.level == "hard":
            hard += 1
            prefix = "error"
        else:
            warn += 1
            prefix = "warning"
        loc = f" ({f.path})" if f.path else ""
        print(f"pack: {prefix}: {f.message}{loc}", file=sys.stderr)
    return hard, warn


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def init_pack(
    dest_dir: str,
    *,
    org: str = "example",
    name: str = "my-pack",
    version: str = "0.1.0",
    description: str = "",
    license_: str = "Apache-2.0",
    force: bool = False,
) -> str:
    """Scaffold ``pack.toml`` (and a README) under ``dest_dir``."""
    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    pack_path = os.path.join(dest_dir, PACK_TOML)
    if os.path.exists(pack_path) and not force:
        raise PackError(f"already exists: {pack_path} (pass --force to overwrite)")
    text = render_pack_toml(
        org=org, name=name, version=version,
        description=description or f"OpenBench task pack {org}/{name}",
        license_=license_,
    )
    with open(pack_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    readme = os.path.join(dest_dir, "README.md")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as fh:
            fh.write(
                f"# {org}/{name}\n\n"
                f"OpenBench task pack `{org}/{name}@{version}`.\n\n"
                "Add task directories next to `pack.toml`, then install with:\n\n"
                f"```bash\nobench pack install {org}/{name}@{version} "
                f"--from {dest_dir}\n```\n"
            )
    return pack_path


def install_pack(
    spec: str,
    source: str,
    *,
    packs_root: str | None = None,
    git_ref: str | None = None,
    git_subdir: str | None = None,
    force: bool = False,
) -> dict:
    """Materialize ``source``, validate, and install under ``packs_root``."""
    packs_root = os.path.abspath(packs_root or default_packs_root())
    staging = tempfile.mkdtemp(prefix="obench-pack-stage-")
    try:
        provenance = materialize_source(
            source, staging, git_ref=git_ref, git_subdir=git_subdir
        )
        # If source was a repo root with pack in a subdir already extracted,
        # pack.toml should be at staging root. If the local dir *is* the pack,
        # same. If someone pointed at a parent, try to find pack.toml.
        pack_root = staging
        if not os.path.isfile(os.path.join(pack_root, PACK_TOML)):
            found = _find_pack_root(staging)
            # Move contents up if nested.
            if found != staging:
                pack_root = found

        meta = load_pack_toml(os.path.join(pack_root, PACK_TOML))
        identity = resolve_install_identity(spec, meta)
        tasks = discover_pack_tasks(pack_root, meta)

        findings = validate_pack_tasks(pack_root, meta)
        hard, _warn = _print_findings(findings)
        if hard:
            raise PackError(
                f"pack failed structure checks ({hard} hard finding(s)); "
                "fix tasks before installing"
            )

        dest = pack_install_dir(
            packs_root, identity["org"], identity["name"], identity["version"]
        )
        if os.path.exists(dest):
            if not force:
                raise PackError(
                    f"already installed: {dest} (pass --force to replace)"
                )
            shutil.rmtree(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        # Final copy into versioned layout.
        shutil.copytree(pack_root, dest, symlinks=False)
        # Drop any nested pack_source from the source tree; rewrite ours.
        provenance = {
            **provenance,
            "identity": pack_identity(identity),
            "org": identity["org"],
            "name": identity["name"],
            "version": identity["version"],
            "tasks": tasks,
            "source": source,
            "digest_scheme": DIGEST_SCHEME_CURRENT,
            "task_digests": {
                t: task_content_digest(
                    os.path.join(dest, t), scheme=DIGEST_SCHEME_CURRENT
                )
                for t in tasks
            },
        }
        write_pack_source(dest, provenance)
        return {
            "dest": dest,
            "identity": pack_identity(identity),
            "tasks": tasks,
            "provenance": provenance,
            "findings": [f.to_dict() for f in findings],
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def list_installed_packs(packs_root: str | None = None) -> list[dict]:
    """Discover installed packs under ``packs_root``."""
    packs_root = os.path.abspath(packs_root or default_packs_root())
    if not os.path.isdir(packs_root):
        return []
    found: list[dict] = []
    for org in sorted(os.listdir(packs_root)):
        org_dir = os.path.join(packs_root, org)
        if not os.path.isdir(org_dir) or org.startswith("."):
            continue
        for name in sorted(os.listdir(org_dir)):
            name_dir = os.path.join(org_dir, name)
            if not os.path.isdir(name_dir) or name.startswith("."):
                continue
            for version in sorted(os.listdir(name_dir)):
                pack_dir = os.path.join(name_dir, version)
                if not os.path.isdir(pack_dir):
                    continue
                toml_path = os.path.join(pack_dir, PACK_TOML)
                if not os.path.isfile(toml_path):
                    continue
                try:
                    meta = load_pack_toml(toml_path)
                    tasks = discover_pack_tasks(pack_dir, meta)
                except PackError as exc:
                    found.append({
                        "org": org,
                        "name": name,
                        "version": version,
                        "identity": f"{org}/{name}@{version}",
                        "path": pack_dir,
                        "error": str(exc),
                    })
                    continue
                source = load_pack_source(pack_dir)
                found.append({
                    "org": meta["org"],
                    "name": meta["name"],
                    "version": meta["version"],
                    "identity": pack_identity(meta),
                    "description": meta.get("description", ""),
                    "license": meta.get("license", ""),
                    "tasks": tasks,
                    "path": pack_dir,
                    "source_kind": (source or {}).get("kind"),
                    "resolved_sha": (source or {}).get("resolved_sha"),
                    "archive_sha256": (source or {}).get("archive_sha256"),
                    "content_sha256": (source or {}).get("content_sha256"),
                })
    return found


def verify_pack(pack_dir: str) -> list[dict]:
    """Recompute task digests and compare to ``pack_source.json`` if present."""
    pack_dir = os.path.abspath(pack_dir)
    meta = load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    tasks = discover_pack_tasks(pack_dir, meta)
    source = load_pack_source(pack_dir)
    expected = (source or {}).get("task_digests") or {}
    scheme = int((source or {}).get("digest_scheme") or DIGEST_SCHEME_CURRENT)
    results = []
    for task in tasks:
        task_dir = os.path.join(pack_dir, task)
        actual = task_content_digest(task_dir, scheme=scheme)
        exp = expected.get(task)
        ok = exp is None or exp == actual
        results.append({
            "task": task,
            "digest": actual,
            "expected": exp,
            "ok": ok,
            "missing_expected": exp is None,
        })
    return results


def resolve_pack_dir(spec: str, packs_root: str | None = None) -> str:
    parsed = parse_pack_spec(spec)
    if parsed["version"] is None:
        raise PackError(
            f"pack verify/list path requires org/name@version; got {spec!r}"
        )
    path = pack_install_dir(
        packs_root or default_packs_root(),
        parsed["org"], parsed["name"], parsed["version"],
    )
    if not os.path.isdir(path):
        raise PackError(f"pack not installed: {spec} (looked in {path})")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obench pack",
        description="Install and manage versioned OpenBench task packs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="scaffold pack.toml in a directory")
    p_init.add_argument(
        "--dir", default=".",
        help="directory for the new pack (default: .)",
    )
    p_init.add_argument("--org", default="example", help="pack org (default: example)")
    p_init.add_argument("--name", default="my-pack", help="pack name (default: my-pack)")
    p_init.add_argument(
        "--version", default="0.1.0", help="pack version (default: 0.1.0)",
    )
    p_init.add_argument("--description", default="", help="short description")
    p_init.add_argument(
        "--license", default="Apache-2.0", dest="license_",
        help="SPDX license id (default: Apache-2.0)",
    )
    p_init.add_argument(
        "--force", action="store_true", help="overwrite existing pack.toml",
    )

    p_install = sub.add_parser(
        "install",
        help="install org/name@version from a local dir, git repo, or https archive",
    )
    p_install.add_argument(
        "spec",
        help="pack identity: org/name or org/name@version",
    )
    p_install.add_argument(
        "--from", dest="source", required=True, metavar="SOURCE",
        help="local directory, git URL (git+https://…[@ref][#subdir]), "
             "or https .zip/.tar.gz URL",
    )
    p_install.add_argument(
        "--packs-dir", default=None,
        help=f"install root (default: ./{DEFAULT_PACKS_DIRNAME})",
    )
    p_install.add_argument(
        "--git-ref", default=None,
        help="git ref override (default: from URL @ref or HEAD)",
    )
    p_install.add_argument(
        "--git-subdir", default=None,
        help="subdirectory inside the git tree that contains pack.toml",
    )
    p_install.add_argument(
        "--force", action="store_true",
        help="replace an existing install of the same version",
    )

    p_list = sub.add_parser("list", help="list installed packs")
    p_list.add_argument(
        "--packs-dir", default=None,
        help=f"packs root (default: ./{DEFAULT_PACKS_DIRNAME})",
    )
    p_list.add_argument(
        "--json", action="store_true", help="emit JSON instead of a table",
    )

    p_verify = sub.add_parser(
        "verify",
        help="recompute task content digests for an installed pack",
    )
    p_verify.add_argument(
        "spec",
        nargs="?",
        default=None,
        help="org/name@version (default: verify every installed pack)",
    )
    p_verify.add_argument(
        "--packs-dir", default=None,
        help=f"packs root (default: ./{DEFAULT_PACKS_DIRNAME})",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            path = init_pack(
                args.dir,
                org=args.org,
                name=args.name,
                version=args.version,
                description=args.description,
                license_=args.license_,
                force=args.force,
            )
            print(f"Wrote {path}")
            print(
                f"Add task directories, then: "
                f"obench pack install {args.org}/{args.name}@{args.version} "
                f"--from {os.path.abspath(args.dir)}"
            )
            return 0

        if args.command == "install":
            info = install_pack(
                args.spec,
                args.source,
                packs_root=args.packs_dir,
                git_ref=args.git_ref,
                git_subdir=args.git_subdir,
                force=args.force,
            )
            print(f"Installed {info['identity']}")
            print(f"  path:  {info['dest']}")
            print(f"  tasks: {', '.join(info['tasks'])}")
            print(
                f"Run: obench validate --tasks-dir {info['dest']}"
            )
            return 0

        if args.command == "list":
            packs = list_installed_packs(args.packs_dir)
            if args.json:
                print(json.dumps(packs, indent=2))
                return 0
            if not packs:
                print("No packs installed.")
                return 0
            for p in packs:
                if p.get("error"):
                    print(f"{p['identity']}  ERROR: {p['error']}")
                    continue
                tasks = ", ".join(p.get("tasks") or [])
                kind = p.get("source_kind") or "?"
                print(f"{p['identity']}  ({kind})  {p['path']}")
                if tasks:
                    print(f"  tasks: {tasks}")
            return 0

        if args.command == "verify":
            packs_root = args.packs_dir
            if args.spec:
                dirs = [resolve_pack_dir(args.spec, packs_root)]
            else:
                packs = list_installed_packs(packs_root)
                dirs = [p["path"] for p in packs if not p.get("error")]
                if not dirs:
                    print("No packs installed.")
                    return 0
            failed = 0
            for pack_dir in dirs:
                meta = load_pack_toml(os.path.join(pack_dir, PACK_TOML))
                identity = pack_identity(meta)
                results = verify_pack(pack_dir)
                print(f"{identity}")
                for row in results:
                    status = "ok" if row["ok"] else "MISMATCH"
                    if row["missing_expected"]:
                        status = "ok (no recorded digest)"
                    if not row["ok"]:
                        failed += 1
                    print(f"  {row['task']}: {status}  {row['digest'][:16]}…")
            return 1 if failed else 0

    except PackError as exc:
        print(f"pack: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
