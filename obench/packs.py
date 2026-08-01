#!/usr/bin/env python3
"""Versioned OpenBench packs (``org/name@version``).

Packs are directories plus a ``pack.toml``. Two kinds share one install layout:

* ``kind = "tasks"`` (default) — task directories (instruction/checker/workspace)
* ``kind = "harness"`` — candidate manifest TOMLs (BYO harnesses)

They install under ``.openbench/packs/<org>/<name>/<version>/`` from local
directories, git repositories, or plain HTTPS zip/tarball URLs — no custom
package server and no new dependencies (stdlib + git CLI for git sources).

    obench pack init [--kind tasks|harness]
    obench pack install org/name@version --from <source>
    obench pack list
    obench pack verify [org/name@version]
    obench pack publish-index --from <pack-dir>

Installed harness manifests are addressable as ``--candidate`` refs:
``org/name@version``, ``org/name`` (latest installed), or
``org/name@version:manifest-stem``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
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
PACKS_INDEX_JSON = "packs.json"
DEFAULT_PACKS_DIRNAME = os.path.join(".openbench", "packs")
PACK_KIND_TASKS = "tasks"
PACK_KIND_HARNESS = "harness"
PACK_KINDS = frozenset({PACK_KIND_TASKS, PACK_KIND_HARNESS})

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
# --candidate pack ref: org/name[@version][:manifest-stem]
_CANDIDATE_REF_RE = re.compile(
    r"^(?P<org>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:@(?P<version>[^:]+))?"
    r"(?::(?P<manifest>[A-Za-z0-9][A-Za-z0-9._-]*))?$"
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

    Required: ``org``, ``name``, ``version``. Optional: ``kind``
    (``tasks`` default, or ``harness``), ``description``, ``license``,
    ``tasks`` / ``manifests`` (explicit lists; omit for auto-discovery).
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

    kind = raw.get("kind", PACK_KIND_TASKS)
    if kind is None:
        kind = PACK_KIND_TASKS
    if not isinstance(kind, str) or kind not in PACK_KINDS:
        raise PackError(
            f"{path}: kind must be one of {sorted(PACK_KINDS)}; got {kind!r}"
        )

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
        if kind != PACK_KIND_TASKS:
            raise PackError(f"{path}: tasks= is only valid for kind=tasks")
        if not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks):
            raise PackError(f"{path}: tasks must be a list of strings when set")
        tasks = [t.strip() for t in tasks]
        if not tasks or any(not t for t in tasks):
            raise PackError(f"{path}: tasks list must be non-empty when set")
        for t in tasks:
            if "/" in t or t in (".", "..") or not _ID_RE.fullmatch(t):
                raise PackError(f"{path}: invalid task name {t!r}")

    manifests = raw.get("manifests")
    if manifests is not None:
        if kind != PACK_KIND_HARNESS:
            raise PackError(f"{path}: manifests= is only valid for kind=harness")
        if not isinstance(manifests, list) or not all(
            isinstance(t, str) for t in manifests
        ):
            raise PackError(f"{path}: manifests must be a list of strings when set")
        manifests = [t.strip() for t in manifests]
        if not manifests or any(not t for t in manifests):
            raise PackError(f"{path}: manifests list must be non-empty when set")
        for m in manifests:
            if "/" in m or m in (".", ".."):
                raise PackError(f"{path}: invalid manifest name {m!r}")
            stem = m[:-5] if m.endswith(".toml") else m
            if not _ID_RE.fullmatch(stem):
                raise PackError(f"{path}: invalid manifest name {m!r}")

    if kind == PACK_KIND_TASKS and manifests is not None:
        raise PackError(f"{path}: manifests= is only valid for kind=harness")
    if kind == PACK_KIND_HARNESS and tasks is not None:
        raise PackError(f"{path}: tasks= is only valid for kind=tasks")

    return {
        "org": org,
        "name": name,
        "version": version,
        "kind": kind,
        "description": description.strip(),
        "license": license_.strip(),
        "tasks": tasks,
        "manifests": manifests,
        "path": path,
    }


def render_pack_toml(
    *,
    org: str,
    name: str,
    version: str,
    kind: str = PACK_KIND_TASKS,
    description: str = "",
    license_: str = "Apache-2.0",
    tasks: list[str] | None = None,
    manifests: list[str] | None = None,
) -> str:
    """Render a ``pack.toml`` scaffold (stdlib — no toml writer dependency)."""
    org = _require_id(org, "org")
    name = _require_id(name, "name")
    version = _require_version(version)
    if kind not in PACK_KINDS:
        raise PackError(f"kind must be one of {sorted(PACK_KINDS)}; got {kind!r}")
    lines = [
        f'org = "{org}"',
        f'name = "{name}"',
        f'version = "{version}"',
        f'kind = "{kind}"',
        f'description = "{_escape_toml_str(description)}"',
        f'license = "{_escape_toml_str(license_)}"',
        "",
    ]
    if kind == PACK_KIND_HARNESS:
        if manifests is not None:
            rendered = ", ".join(f'"{_escape_toml_str(t)}"' for t in manifests)
            lines.append(f"manifests = [{rendered}]")
            lines.append("")
        else:
            lines.append(
                "# manifests = [\"my-cli.toml\"]  # omit to auto-discover *.toml"
            )
            lines.append("")
    elif tasks is not None:
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
    """Return task directory names for a tasks pack (explicit list or auto-discover)."""
    pack_dir = os.path.abspath(pack_dir)
    meta = meta or load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    if meta.get("kind", PACK_KIND_TASKS) != PACK_KIND_TASKS:
        raise PackError(
            f"discover_pack_tasks requires kind=tasks; got kind={meta.get('kind')!r}"
        )
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


def _normalize_manifest_filename(name: str) -> str:
    name = name.strip()
    if not name.endswith(".toml"):
        name = f"{name}.toml"
    return name


def discover_pack_manifests(pack_dir: str, meta: dict | None = None) -> list[str]:
    """Return candidate manifest filenames for a harness pack."""
    pack_dir = os.path.abspath(pack_dir)
    meta = meta or load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    if meta.get("kind") != PACK_KIND_HARNESS:
        raise PackError(
            f"discover_pack_manifests requires kind=harness; "
            f"got kind={meta.get('kind')!r}"
        )
    if meta.get("manifests") is not None:
        names = [_normalize_manifest_filename(n) for n in meta["manifests"]]
        missing = [
            n for n in names
            if not os.path.isfile(os.path.join(pack_dir, n))
        ]
        if missing:
            raise PackError(
                f"pack.toml manifests not found under {pack_dir}: "
                f"{', '.join(missing)}"
            )
        return names

    names = []
    for entry in sorted(os.listdir(pack_dir)):
        if entry.startswith(".") or entry == PACK_TOML:
            continue
        if not entry.endswith(".toml"):
            continue
        path = os.path.join(pack_dir, entry)
        if os.path.isfile(path):
            names.append(entry)
    if not names:
        raise PackError(
            f"no candidate manifests (*.toml) found under pack directory: {pack_dir}"
        )
    return names


def pack_identity(meta: dict) -> str:
    return f"{meta['org']}/{meta['name']}@{meta['version']}"


def _semver_sort_key(version: str):
    """Sort key for installed versions (best-effort semver; stdlib-only)."""
    version = version.strip()
    main, _, pre = version.partition("-")
    main = main.split("+", 1)[0]
    parts = []
    for piece in main.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    # Pre-release sorts before the matching release.
    return (parts[0], parts[1], parts[2], pre == "", pre)


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
    """Project-scoped ``.openbench/packs`` discovered from any subdirectory."""
    from .config import load_config
    cfg = load_config(start)
    base = cfg.project_root
    if base is None:
        base = os.path.abspath(start or os.getcwd())
        probe = base
        while True:
            if os.path.isdir(os.path.join(probe, "tasks")):
                base = probe
                break
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
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


def _sha256_tree(root: str, *, exclude_names: frozenset[str] | None = None) -> str:
    """Stable content digest of a directory tree (paths relative to root)."""
    root = os.path.abspath(root)
    exclude_names = exclude_names or frozenset()
    hasher = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or name in exclude_names:
                continue
            full = os.path.join(dirpath, name)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            # Only exclude well-known install artifacts at the pack root.
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in exclude_names:
                continue
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
    dest = os.path.abspath(dest)

    def safe_destination(name: str) -> str:
        normalized = name.replace("\\", "/")
        drive, _tail = ntpath.splitdrive(normalized)
        if (
            not normalized
            or "\x00" in normalized
            or drive
            or normalized.startswith("/")
        ):
            raise PackError(f"unsafe archive member path in {url}: {name!r}")
        target = os.path.abspath(os.path.join(dest, normalized))
        try:
            contained = os.path.commonpath((dest, target)) == dest
        except ValueError:
            contained = False
        if not contained:
            raise PackError(f"unsafe archive member path in {url}: {name!r}")
        return target

    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive_path) as zf:
                for member in zf.infolist():
                    safe_destination(member.filename)
                    mode = (member.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        raise PackError(
                            f"archive links are not allowed in {url}: "
                            f"{member.filename!r}"
                        )
                zf.extractall(dest)
        except zipfile.BadZipFile as exc:
            raise PackError(f"invalid zip archive: {url}") from exc
        return
    try:
        with tarfile.open(archive_path) as tf:
            members = tf.getmembers()
            for member in members:
                safe_destination(member.name)
                if not (member.isfile() or member.isdir()):
                    raise PackError(
                        f"only regular files and directories are allowed in "
                        f"{url}: {member.name!r}"
                    )
            # Python 3.11.4+ provides the data filter. Earlier supported
            # runtimes use the same prevalidated member list, never an
            # unrestricted extraction fallback.
            try:
                tf.extractall(dest, members=members, filter="data")
            except TypeError:
                tf.extractall(dest, members=members)
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


def validate_pack_manifests(
    pack_dir: str,
    meta: dict | None = None,
    *,
    adapters_dir: str | None = None,
) -> list[Finding]:
    """Load each harness-pack manifest through the candidate schema."""
    from .candidates import load_candidate
    from .paths import default_adapters_dir

    pack_dir = os.path.abspath(pack_dir)
    meta = meta or load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    adapters_dir = adapters_dir or default_adapters_dir()
    findings: list[Finding] = []
    for filename in discover_pack_manifests(pack_dir, meta):
        path = os.path.join(pack_dir, filename)
        try:
            load_candidate(path, adapters_dir)
        except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
            findings.append(Finding(
                "harness.manifest",
                "hard",
                f"{filename}: invalid candidate manifest: {exc}",
                path=filename,
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


def manifest_spec_sha256(path: str) -> str:
    """SHA-256 of a candidate manifest file (matches ``spec_sha256`` provenance)."""
    return _sha256_file(path)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def init_pack(
    dest_dir: str,
    *,
    org: str = "example",
    name: str = "my-pack",
    version: str = "0.1.0",
    kind: str = PACK_KIND_TASKS,
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
    if kind not in PACK_KINDS:
        raise PackError(f"kind must be one of {sorted(PACK_KINDS)}; got {kind!r}")
    label = "harness pack" if kind == PACK_KIND_HARNESS else "task pack"
    text = render_pack_toml(
        org=org, name=name, version=version, kind=kind,
        description=description or f"OpenBench {label} {org}/{name}",
        license_=license_,
    )
    with open(pack_path, "w", encoding="utf-8") as fh:
        fh.write(text)

    readme = os.path.join(dest_dir, "README.md")
    if not os.path.exists(readme):
        if kind == PACK_KIND_HARNESS:
            body = (
                f"# {org}/{name}\n\n"
                f"OpenBench harness pack `{org}/{name}@{version}`.\n\n"
                "Add candidate manifest ``.toml`` files next to `pack.toml`, "
                "then install with:\n\n"
                f"```bash\nobench pack install {org}/{name}@{version} "
                f"--from {dest_dir}\n"
                f"obench doctor --candidate {org}/{name}@{version} "
                f"--model <model>\n```\n"
            )
        else:
            body = (
                f"# {org}/{name}\n\n"
                f"OpenBench task pack `{org}/{name}@{version}`.\n\n"
                "Add task directories next to `pack.toml`, then install with:\n\n"
                f"```bash\nobench pack install {org}/{name}@{version} "
                f"--from {dest_dir}\n```\n"
            )
        with open(readme, "w", encoding="utf-8") as fh:
            fh.write(body)
    return pack_path


def install_pack(
    spec: str,
    source: str,
    *,
    packs_root: str | None = None,
    git_ref: str | None = None,
    git_subdir: str | None = None,
    force: bool = False,
    adapters_dir: str | None = None,
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
        kind = meta.get("kind", PACK_KIND_TASKS)

        if kind == PACK_KIND_HARNESS:
            members = discover_pack_manifests(pack_root, meta)
            findings = validate_pack_manifests(
                pack_root, meta, adapters_dir=adapters_dir
            )
            member_key = "manifests"
            fail_label = "manifest checks"
        else:
            members = discover_pack_tasks(pack_root, meta)
            findings = validate_pack_tasks(pack_root, meta)
            member_key = "tasks"
            fail_label = "structure checks"

        hard, _warn = _print_findings(findings)
        if hard:
            raise PackError(
                f"pack failed {fail_label} ({hard} hard finding(s)); "
                "fix the pack before installing"
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
        nested_source = os.path.join(dest, PACK_SOURCE_JSON)
        if os.path.isfile(nested_source):
            os.remove(nested_source)
        # Tree hash of installed payload (excludes pack_source.json).
        tree_sha = _sha256_tree(dest)
        # Keep materialization ``kind`` (dir/git/https); pack kind is pack_kind.
        provenance = {
            **provenance,
            "identity": pack_identity(identity),
            "org": identity["org"],
            "name": identity["name"],
            "version": identity["version"],
            "pack_kind": kind,
            member_key: members,
            "source": source,
            "content_sha256": tree_sha,
        }
        if kind == PACK_KIND_HARNESS:
            # Mirror candidate ``spec_sha256`` per manifest file.
            digests = {
                m: manifest_spec_sha256(os.path.join(dest, m))
                for m in members
            }
            provenance["manifest_digests"] = digests
            provenance["spec_sha256"] = dict(digests)
        else:
            provenance["digest_scheme"] = DIGEST_SCHEME_CURRENT
            provenance["task_digests"] = {
                t: task_content_digest(
                    os.path.join(dest, t), scheme=DIGEST_SCHEME_CURRENT
                )
                for t in members
            }
        write_pack_source(dest, provenance)
        result = {
            "dest": dest,
            "identity": pack_identity(identity),
            "kind": kind,
            member_key: members,
            "provenance": provenance,
            "findings": [f.to_dict() for f in findings],
        }
        return result
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
                    kind = meta.get("kind", PACK_KIND_TASKS)
                    if kind == PACK_KIND_HARNESS:
                        members = discover_pack_manifests(pack_dir, meta)
                    else:
                        members = discover_pack_tasks(pack_dir, meta)
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
                entry = {
                    "org": meta["org"],
                    "name": meta["name"],
                    "version": meta["version"],
                    "kind": kind,
                    "identity": pack_identity(meta),
                    "description": meta.get("description", ""),
                    "license": meta.get("license", ""),
                    "path": pack_dir,
                    "source_kind": (source or {}).get("kind"),
                    "resolved_sha": (source or {}).get("resolved_sha"),
                    "archive_sha256": (source or {}).get("archive_sha256"),
                    "content_sha256": (source or {}).get("content_sha256"),
                }
                if kind == PACK_KIND_HARNESS:
                    entry["manifests"] = members
                else:
                    entry["tasks"] = members
                found.append(entry)
    return found


def verify_pack(pack_dir: str) -> list[dict]:
    """Recompute digests and fail closed without recorded expectations."""
    pack_dir = os.path.abspath(pack_dir)
    meta = load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    kind = meta.get("kind", PACK_KIND_TASKS)
    source = load_pack_source(pack_dir)
    results = []
    if kind == PACK_KIND_HARNESS:
        manifests = discover_pack_manifests(pack_dir, meta)
        expected = (source or {}).get("manifest_digests") or (
            (source or {}).get("spec_sha256") or {}
        )
        for filename in manifests:
            actual = manifest_spec_sha256(os.path.join(pack_dir, filename))
            exp = expected.get(filename)
            results.append({
                "manifest": filename,
                "digest": actual,
                "expected": exp,
                "ok": exp is not None and exp == actual,
                "missing_expected": exp is None,
            })
        for filename in sorted(set(expected) - set(manifests)):
            results.append({
                "manifest": filename,
                "digest": "",
                "expected": expected[filename],
                "ok": False,
                "missing_expected": False,
                "missing_member": True,
            })
        return results

    tasks = discover_pack_tasks(pack_dir, meta)
    expected = (source or {}).get("task_digests") or {}
    scheme = int((source or {}).get("digest_scheme") or DIGEST_SCHEME_CURRENT)
    for task in tasks:
        task_dir = os.path.join(pack_dir, task)
        actual = task_content_digest(task_dir, scheme=scheme)
        exp = expected.get(task)
        results.append({
            "task": task,
            "digest": actual,
            "expected": exp,
            "ok": exp is not None and exp == actual,
            "missing_expected": exp is None,
        })
    for task in sorted(set(expected) - set(tasks)):
        results.append({
            "task": task,
            "digest": "",
            "expected": expected[task],
            "ok": False,
            "missing_expected": False,
            "missing_member": True,
        })
    return results


def resolve_pack_dir(spec: str, packs_root: str | None = None) -> str:
    parsed = parse_pack_spec(spec)
    if parsed["version"] is None:
        # Latest installed version for org/name.
        return resolve_installed_pack_dir(
            parsed["org"], parsed["name"], version=None, packs_root=packs_root
        )
    path = pack_install_dir(
        packs_root or default_packs_root(),
        parsed["org"], parsed["name"], parsed["version"],
    )
    if not os.path.isdir(path):
        raise PackError(f"pack not installed: {spec} (looked in {path})")
    return path


def list_installed_versions(
    org: str, name: str, packs_root: str | None = None
) -> list[str]:
    """Return installed versions for ``org/name`` (semver-sorted ascending)."""
    packs_root = os.path.abspath(packs_root or default_packs_root())
    name_dir = os.path.join(
        packs_root, _require_id(org, "org"), _require_id(name, "name")
    )
    if not os.path.isdir(name_dir):
        return []
    versions = []
    for entry in os.listdir(name_dir):
        pack_dir = os.path.join(name_dir, entry)
        if not os.path.isdir(pack_dir):
            continue
        if not os.path.isfile(os.path.join(pack_dir, PACK_TOML)):
            continue
        if _VERSION_RE.fullmatch(entry):
            versions.append(entry)
    versions.sort(key=_semver_sort_key)
    return versions


def resolve_installed_pack_dir(
    org: str,
    name: str,
    *,
    version: str | None = None,
    packs_root: str | None = None,
) -> str:
    packs_root = packs_root or default_packs_root()
    if version is None:
        versions = list_installed_versions(org, name, packs_root)
        if not versions:
            raise PackError(
                f"pack not installed: {org}/{name} "
                f"(looked under {os.path.abspath(packs_root)})"
            )
        version = versions[-1]
    else:
        version = _require_version(version)
    path = pack_install_dir(packs_root, org, name, version)
    if not os.path.isdir(path):
        raise PackError(
            f"pack not installed: {org}/{name}@{version} (looked in {path})"
        )
    return path


def resolve_candidate_ref(ref: str, packs_root: str | None = None) -> str:
    """Resolve a ``--candidate`` arg to an absolute manifest path.

    Accepts:

    * filesystem path to a ``.toml`` (existing behavior)
    * pack ref ``org/name[@version][:manifest-stem]`` for an installed
      harness pack (``:manifest`` required when the pack has multiple
      manifests; omitted ``@version`` selects the latest installed)
    """
    if not isinstance(ref, str) or not ref.strip():
        raise PackError("candidate ref must be a non-empty string")
    ref = ref.strip()
    if os.path.isfile(ref):
        return os.path.abspath(ref)

    match = _CANDIDATE_REF_RE.fullmatch(ref)
    if not match:
        # Preserve a clear path error for typos that look like files.
        raise PackError(
            f"candidate not found: {ref!r} (not a file; expected path or "
            "harness pack ref org/name[@version][:manifest])"
        )

    org = match.group("org")
    name = match.group("name")
    version = match.group("version")
    manifest_sel = match.group("manifest")
    if version is not None:
        version = version.strip()
        if not _VERSION_RE.fullmatch(version):
            raise PackError(
                f"invalid pack version {version!r} in candidate ref {ref!r}"
            )

    pack_dir = resolve_installed_pack_dir(
        org, name, version=version, packs_root=packs_root
    )
    meta = load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    if meta.get("kind") != PACK_KIND_HARNESS:
        raise PackError(
            f"{meta['org']}/{meta['name']}@{meta['version']} is kind="
            f"{meta.get('kind')!r}; --candidate pack refs require kind=harness"
        )
    manifests = discover_pack_manifests(pack_dir, meta)
    if manifest_sel:
        want = _normalize_manifest_filename(manifest_sel)
        if want not in manifests:
            raise PackError(
                f"manifest {want!r} not in pack {pack_identity(meta)}; "
                f"have {manifests}"
            )
        return os.path.join(pack_dir, want)
    if len(manifests) == 1:
        return os.path.join(pack_dir, manifests[0])
    raise PackError(
        f"pack {pack_identity(meta)} has multiple manifests {manifests}; "
        f"pass org/name@version:manifest-stem (e.g. "
        f"{meta['org']}/{meta['name']}@{meta['version']}:"
        f"{manifests[0][:-5]})"
    )


# ---------------------------------------------------------------------------
# Site index (docs/packs.json)
# ---------------------------------------------------------------------------


def load_packs_index(site_dir: str) -> list:
    """Read ``packs.json`` from a site dir; missing → empty list."""
    path = os.path.join(site_dir, PACKS_INDEX_JSON)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise PackError(f"{PACKS_INDEX_JSON} must contain a JSON list")
    return data


def write_packs_index(site_dir: str, entries: list) -> str:
    path = os.path.join(os.path.abspath(site_dir), PACKS_INDEX_JSON)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def pack_index_entry(
    pack_dir: str,
    *,
    source: str | None = None,
) -> dict:
    """Build one ``packs.json`` entry from a local pack directory."""
    pack_dir = os.path.abspath(pack_dir)
    meta = load_pack_toml(os.path.join(pack_dir, PACK_TOML))
    kind = meta.get("kind", PACK_KIND_TASKS)
    # Hash payload excluding install-time provenance.
    content_sha = _sha256_tree(
        pack_dir, exclude_names=frozenset({PACK_SOURCE_JSON})
    )

    entry = {
        "id": f"{meta['org']}/{meta['name']}",
        "org": meta["org"],
        "name": meta["name"],
        "latest": meta["version"],
        "kind": kind,
        "description": meta.get("description", ""),
        "license": meta.get("license", ""),
        "source": source or pack_dir,
        "content_sha256": content_sha,
    }
    if kind == PACK_KIND_HARNESS:
        entry["manifests"] = discover_pack_manifests(pack_dir, meta)
    else:
        entry["tasks"] = discover_pack_tasks(pack_dir, meta)
    return entry


def publish_packs_index(
    pack_dir: str,
    *,
    site_dir: str | None = None,
    source: str | None = None,
) -> dict:
    """Upsert a pack into ``site_dir/packs.json`` and refresh ``index.html``."""
    from . import community as community_mod

    site_dir = os.path.abspath(site_dir or os.path.join(os.getcwd(), "docs"))
    entry = pack_index_entry(pack_dir, source=source)
    entries = load_packs_index(site_dir)
    replaced = False
    for i, existing in enumerate(entries):
        if existing.get("id") == entry["id"]:
            entries[i] = entry
            replaced = True
            break
    if not replaced:
        entries.append(entry)
    entries.sort(key=lambda e: (e.get("id") or ""))
    manifest_path = write_packs_index(site_dir, entries)
    index_path = community_mod.write_site_index(site_dir, packs=entries)
    return {
        "entry": entry,
        "manifest_path": manifest_path,
        "index_path": index_path,
        "count": len(entries),
        "replaced": replaced,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obench pack",
        description="Install and manage versioned OpenBench packs "
                    "(tasks or harness manifests).",
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
    p_init.add_argument(
        "--kind", default=PACK_KIND_TASKS, choices=sorted(PACK_KINDS),
        help="pack kind: tasks (default) or harness",
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
        help="recompute content digests for an installed pack",
    )
    p_verify.add_argument(
        "spec",
        nargs="?",
        default=None,
        help="org/name or org/name@version (default: verify every installed pack)",
    )
    p_verify.add_argument(
        "--packs-dir", default=None,
        help=f"packs root (default: ./{DEFAULT_PACKS_DIRNAME})",
    )

    p_pub = sub.add_parser(
        "publish-index",
        help="upsert a local pack into docs/packs.json and refresh the site index",
    )
    p_pub.add_argument(
        "--from", dest="source", required=True, metavar="DIR",
        help="local pack directory containing pack.toml",
    )
    p_pub.add_argument(
        "--site-dir", default=None,
        help="site root with packs.json / index.html (default: ./docs)",
    )
    p_pub.add_argument(
        "--source-url", default=None,
        help="source URL or path recorded in packs.json (default: --from path)",
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            path = init_pack(
                args.dir,
                org=args.org,
                name=args.name,
                version=args.version,
                kind=args.kind,
                description=args.description,
                license_=args.license_,
                force=args.force,
            )
            print(f"Wrote {path}")
            if args.kind == PACK_KIND_HARNESS:
                print(
                    f"Add candidate .toml manifests, then: "
                    f"obench pack install {args.org}/{args.name}@{args.version} "
                    f"--from {os.path.abspath(args.dir)}"
                )
            else:
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
            print(f"Installed {info['identity']}  (kind={info['kind']})")
            print(f"  path:  {info['dest']}")
            if info["kind"] == PACK_KIND_HARNESS:
                manifests = ", ".join(info.get("manifests") or [])
                print(f"  manifests: {manifests}")
                print(
                    f"Run: obench doctor --candidate {info['identity']} --model <model>"
                )
            else:
                print(f"  tasks: {', '.join(info.get('tasks') or [])}")
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
                src_kind = p.get("source_kind") or "?"
                kind = p.get("kind") or PACK_KIND_TASKS
                print(f"{p['identity']}  [{kind}] ({src_kind})  {p['path']}")
                if kind == PACK_KIND_HARNESS:
                    members = ", ".join(p.get("manifests") or [])
                    if members:
                        print(f"  manifests: {members}")
                else:
                    tasks = ", ".join(p.get("tasks") or [])
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
                print(f"{identity}  [{meta.get('kind', PACK_KIND_TASKS)}]")
                for row in results:
                    status = "ok" if row["ok"] else "MISMATCH"
                    if row["missing_expected"]:
                        status = "MISSING EXPECTED DIGEST"
                    if not row["ok"]:
                        failed += 1
                    label = row.get("task") or row.get("manifest") or "?"
                    print(f"  {label}: {status}  {row['digest'][:16]}…")
            return 1 if failed else 0

        if args.command == "publish-index":
            info = publish_packs_index(
                args.source,
                site_dir=args.site_dir,
                source=args.source_url or args.source,
            )
            action = "Updated" if info["replaced"] else "Added"
            entry = info["entry"]
            print(
                f"{action} {entry['id']}@{entry['latest']} "
                f"(kind={entry['kind']}) in {info['manifest_path']}"
            )
            print(f"  content_sha256: {entry['content_sha256']}")
            print(f"  index: {info['index_path']}")
            return 0

    except PackError as exc:
        print(f"pack: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
