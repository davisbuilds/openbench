"""Task workspace materialization: snapshot ``workspace/`` or git ``workspace.toml``.

A task provides exactly one of:

- ``workspace/`` — static snapshot, ``shutil``-copied per trial (legacy / small tasks)
- ``workspace.toml`` — materialize from a git ref (+ optional subdir / setup script)

Having both is a validation error. Git mode uses ``git archive`` (export without
``.git``) so the source repo is never mutated and no worktrees are left behind.
Remote URLs are shallow-cloned into a disposable temp dir, archived, then deleted.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import warnings
from dataclasses import dataclass


WORKSPACE_TOML = "workspace.toml"
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


class WorkspaceError(Exception):
    """Raised when workspace config is invalid or materialization fails."""


@dataclass(frozen=True)
class GitWorkspaceSpec:
    """Parsed ``workspace.toml`` (kind = git)."""

    repo: str
    ref: str
    subdir: str | None = None
    setup: str | None = None
    depth: int | None = None


def workspace_dir(task_dir: str) -> str:
    return os.path.join(task_dir, "workspace")


def workspace_toml_path(task_dir: str) -> str:
    return os.path.join(task_dir, WORKSPACE_TOML)


def has_snapshot_workspace(task_dir: str) -> bool:
    return os.path.isdir(workspace_dir(task_dir))


def has_git_workspace(task_dir: str) -> bool:
    return os.path.isfile(workspace_toml_path(task_dir))


def resolve_workspace_mode(task_dir: str) -> str:
    """Return ``\"snapshot\"`` or ``\"git\"``; raise if both or neither."""
    snap = has_snapshot_workspace(task_dir)
    git = has_git_workspace(task_dir)
    if snap and git:
        raise WorkspaceError(
            "task has both workspace/ and workspace.toml; provide exactly one"
        )
    if git:
        return "git"
    if snap:
        return "snapshot"
    raise WorkspaceError(
        "task has neither workspace/ nor workspace.toml; provide exactly one"
    )


def _is_remote_repo(repo: str) -> bool:
    if "://" in repo:
        return True
    if repo.startswith("git@"):
        return True
    return False


def _normalize_subdir(subdir: str | None) -> str | None:
    if subdir is None:
        return None
    if not isinstance(subdir, str) or not subdir.strip():
        raise WorkspaceError("subdir must be a non-empty string when set")
    cleaned = subdir.strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise WorkspaceError("subdir must be a non-empty string when set")
    parts = cleaned.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise WorkspaceError(
            f"subdir must be a relative path without '.' or '..': {subdir!r}"
        )
    return cleaned


def load_git_workspace_spec(task_dir: str) -> GitWorkspaceSpec:
    """Parse and validate ``workspace.toml`` for a git-mode task."""
    path = workspace_toml_path(task_dir)
    if not os.path.isfile(path):
        raise WorkspaceError(f"missing {WORKSPACE_TOML}")
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise WorkspaceError(f"invalid {WORKSPACE_TOML}: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkspaceError(f"{WORKSPACE_TOML} must be a TOML table")

    kind = raw.get("kind", "git")
    if kind != "git":
        raise WorkspaceError(
            f"{WORKSPACE_TOML} kind must be \"git\" (got {kind!r})"
        )

    if "ref" not in raw:
        raise WorkspaceError(f"{WORKSPACE_TOML} missing required field: ref")
    ref = raw.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise WorkspaceError(f"{WORKSPACE_TOML} ref must be a non-empty string")
    ref = ref.strip()

    repo = raw.get("repo", ".")
    if not isinstance(repo, str) or not repo.strip():
        raise WorkspaceError(f"{WORKSPACE_TOML} repo must be a non-empty string")
    repo = repo.strip()

    subdir = _normalize_subdir(raw.get("subdir"))

    setup = raw.get("setup")
    if setup is not None:
        if not isinstance(setup, str) or not setup.strip():
            raise WorkspaceError("setup must be a non-empty string when set")
        setup = setup.strip().replace("\\", "/")
        if setup.startswith("/") or any(p == ".." for p in setup.split("/")):
            raise WorkspaceError(
                f"setup must be a task-relative path without '..': {setup!r}"
            )

    depth = raw.get("depth")
    if depth is not None:
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
            raise WorkspaceError("depth must be a positive integer when set")

    unknown = set(raw) - {"kind", "repo", "ref", "subdir", "setup", "depth"}
    if unknown:
        raise WorkspaceError(
            f"{WORKSPACE_TOML} has unknown field(s): {', '.join(sorted(unknown))}"
        )

    return GitWorkspaceSpec(
        repo=repo, ref=ref, subdir=subdir, setup=setup, depth=depth,
    )


def find_git_root(start: str) -> str:
    """Walk upward from ``start`` to the containing git work tree."""
    cur = os.path.abspath(start)
    while True:
        git_entry = os.path.join(cur, ".git")
        if os.path.isdir(git_entry) or os.path.isfile(git_entry):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            raise WorkspaceError(
                f"not a git repository (or any parent): {os.path.abspath(start)}"
            )
        cur = parent


def resolve_repo_path(task_dir: str, repo: str) -> str:
    """Resolve a local ``repo`` value to an absolute git root.

    ``\".\"`` means the git repository that contains the task directory.
    Other relative paths are resolved against the task directory, then the
    containing git root of that path is used when the path itself is not a
    repo root.
    """
    if _is_remote_repo(repo):
        raise WorkspaceError(f"internal error: resolve_repo_path on remote {repo!r}")
    task_dir = os.path.abspath(task_dir)
    if repo == ".":
        return find_git_root(task_dir)
    if os.path.isabs(repo):
        path = os.path.abspath(repo)
    else:
        path = os.path.abspath(os.path.join(task_dir, repo))
    if not os.path.exists(path):
        raise WorkspaceError(f"repo path does not exist: {path}")
    # Prefer the path if it is itself a git root; otherwise find containing root.
    git_entry = os.path.join(path, ".git")
    if os.path.isdir(git_entry) or os.path.isfile(git_entry):
        return path
    return find_git_root(path)


def _run_git(args: list[str], *, cwd: str | None = None) -> str:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorkspaceError(
            "git CLI not found on PATH; install git to use workspace.toml"
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise WorkspaceError(err)
    return (proc.stdout or "").strip()


def _assert_git_repo(repo_path: str) -> None:
    try:
        out = _run_git(
            ["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"]
        )
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"not a git repository: {repo_path} ({exc})"
        ) from exc
    if out != "true":
        raise WorkspaceError(f"not a git repository: {repo_path}")


def resolve_commit_sha(repo_path: str, ref: str) -> str:
    """Resolve ``ref`` to a full commit SHA inside ``repo_path``."""
    _assert_git_repo(repo_path)
    try:
        return _run_git(
            ["git", "-C", repo_path, "rev-parse", "--verify", f"{ref}^{{commit}}"]
        )
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"unknown git ref {ref!r} in {repo_path}: {exc}"
        ) from exc


def _warn_non_sha(ref: str, resolved_sha: str) -> None:
    if FULL_SHA_RE.fullmatch(ref):
        return
    msg = (
        f"workspace.toml ref={ref!r} is not a full 40-char commit SHA; "
        f"resolved to {resolved_sha} (pin the SHA for reproducibility)"
    )
    print(f"obench: warning: {msg}", file=sys.stderr)
    warnings.warn(msg, UserWarning, stacklevel=3)


def _git_archive_extract(repo_path: str, sha: str, dest: str, subdir: str | None) -> None:
    """Export ``sha`` (optional ``subdir`` as workspace root) into ``dest``.

    Uses ``git archive`` so the staged tree has no ``.git`` metadata and the
    source repository is never mutated (no worktrees, no checkouts).
    """
    treeish = f"{sha}:{subdir}" if subdir else sha
    # Confirm the path exists for a clearer error than tar's empty extract.
    if subdir:
        try:
            kind = _run_git(
                ["git", "-C", repo_path, "cat-file", "-t", treeish]
            )
        except WorkspaceError as exc:
            raise WorkspaceError(
                f"subdir {subdir!r} not found at ref {sha[:12]}: {exc}"
            ) from exc
        if kind != "tree":
            raise WorkspaceError(
                f"subdir {subdir!r} at ref {sha[:12]} is a {kind}, not a directory"
            )

    archive = subprocess.Popen(
        ["git", "-C", repo_path, "archive", "--format=tar", treeish],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    tar = subprocess.Popen(
        ["tar", "-x", "-C", dest],
        stdin=archive.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    archive.stdout.close()
    _tar_out, tar_err = tar.communicate()
    _arch_out, arch_err = archive.communicate()
    if archive.returncode != 0:
        err = (arch_err or b"").decode("utf-8", "replace").strip()
        raise WorkspaceError(
            f"git archive failed for {treeish!r}: {err or archive.returncode}"
        )
    if tar.returncode != 0:
        err = (tar_err or b"").decode("utf-8", "replace").strip()
        raise WorkspaceError(
            f"tar extract failed for git archive: {err or tar.returncode}"
        )


def _clone_remote(url: str, dest: str, ref: str, depth: int | None) -> None:
    """Clone ``url`` into ``dest`` such that ``ref`` is resolvable."""
    # Prefer a depth-limited fetch of the requested ref when possible.
    _run_git(["git", "init", "--quiet", dest])
    _run_git(["git", "-C", dest, "remote", "add", "origin", url])
    fetch_cmd = ["git", "-C", dest, "fetch", "--quiet"]
    if depth is not None:
        fetch_cmd += ["--depth", str(depth)]
    fetch_cmd += ["origin", ref]
    try:
        _run_git(fetch_cmd)
    except WorkspaceError:
        # Some hosts refuse arbitrary-SHA shallow fetches; retry without depth,
        # then fall back to a full fetch of the ref tip.
        if depth is not None:
            try:
                _run_git(["git", "-C", dest, "fetch", "--quiet", "origin", ref])
            except WorkspaceError:
                _run_git(["git", "-C", dest, "fetch", "--quiet", "origin"])
        else:
            _run_git(["git", "-C", dest, "fetch", "--quiet", "origin"])


def _run_setup_script(task_dir: str, workdir: str, setup_rel: str) -> None:
    script = os.path.normpath(os.path.join(task_dir, setup_rel))
    task_real = os.path.realpath(task_dir)
    script_real = os.path.realpath(script)
    try:
        common = os.path.commonpath([script_real, task_real])
    except ValueError:
        common = ""
    if common != task_real or not os.path.isfile(script_real):
        raise WorkspaceError(
            f"setup script not found under task dir: {setup_rel!r}"
        )
    env = dict(os.environ)
    env["TASK_DIR"] = os.path.abspath(task_dir)
    try:
        proc = subprocess.run(
            ["bash", script_real],
            cwd=workdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorkspaceError("bash not found on PATH; needed for setup scripts") from exc
    if proc.returncode != 0:
        out = (proc.stdout or "").strip()
        detail = f"\n{out}" if out else ""
        raise WorkspaceError(
            f"setup script {setup_rel!r} failed with exit {proc.returncode}{detail}"
        )


def _clear_dir(path: str) -> None:
    for name in os.listdir(path):
        target = os.path.join(path, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        else:
            os.unlink(target)


def _copy_snapshot_into(task_dir: str, dest: str) -> None:
    src = workspace_dir(task_dir)
    _clear_dir(dest)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dest, name)
        if os.path.isdir(s) and not os.path.islink(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


def materialize_git_workspace(task_dir: str, dest: str, spec: GitWorkspaceSpec | None = None) -> dict:
    """Materialize a git-mode workspace into ``dest``.

    Returns a provenance dict suitable for ``workspace_source`` on a results row.
    Always cleans remote clone temps; never mutates the source repository.
    """
    task_dir = os.path.abspath(task_dir)
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    _clear_dir(dest)

    if spec is None:
        spec = load_git_workspace_spec(task_dir)

    clone_dir = None
    try:
        if _is_remote_repo(spec.repo):
            clone_dir = tempfile.mkdtemp(prefix="obench_git_clone_")
            _clone_remote(spec.repo, clone_dir, spec.ref, spec.depth)
            repo_path = clone_dir
        else:
            repo_path = resolve_repo_path(task_dir, spec.repo)

        resolved_sha = resolve_commit_sha(repo_path, spec.ref)
        _warn_non_sha(spec.ref, resolved_sha)
        _git_archive_extract(repo_path, resolved_sha, dest, spec.subdir)

        if spec.setup:
            _run_setup_script(task_dir, dest, spec.setup)

        # Default export has no .git; assert that for the archive path.
        if os.path.exists(os.path.join(dest, ".git")):
            raise WorkspaceError(
                "internal error: staged workspace unexpectedly contains .git"
            )

        provenance = {
            "kind": "git",
            "repo": spec.repo,
            "ref": spec.ref,
            "resolved_sha": resolved_sha,
        }
        if spec.subdir:
            provenance["subdir"] = spec.subdir
        return provenance
    finally:
        if clone_dir is not None:
            shutil.rmtree(clone_dir, ignore_errors=True)


def materialize_workspace(task_dir: str, dest: str) -> dict | None:
    """Fill ``dest`` with the task's starting workspace.

    Returns git provenance dict, or ``None`` for snapshot mode.
    """
    task_dir = os.path.abspath(task_dir)
    dest = os.path.abspath(dest)
    mode = resolve_workspace_mode(task_dir)
    if mode == "snapshot":
        _copy_snapshot_into(task_dir, dest)
        return None
    return materialize_git_workspace(task_dir, dest)


def overlay_solution(task_dir: str, dest: str) -> None:
    """Copy ``solution/`` contents on top of an already-staged workspace."""
    solution = os.path.join(task_dir, "solution")
    if not os.path.isdir(solution):
        return
    for root, _dirs, files in os.walk(solution):
        rel = os.path.relpath(root, solution)
        target_root = dest if rel == "." else os.path.join(dest, rel)
        os.makedirs(target_root, exist_ok=True)
        for name in files:
            shutil.copy2(os.path.join(root, name), os.path.join(target_root, name))


GIT_WORKSPACE_TOML_TEMPLATE = """\
# Materialize the agent workspace from a git ref (not a static workspace/ copy).
# Pin ref to a full commit SHA for reproducible runs.

kind = "git"

# "." = the git repo containing this task directory.
# Absolute/relative local path, or a git URL for a remote.
repo = "."

# Commit SHA (recommended), tag, or branch.
ref = "{ref}"

# Optional: only this subtree becomes the workspace root.
{subdir_line}

# Optional: task-relative script run in the staged workspace after checkout
# (cwd = workspace, TASK_DIR set). Must exit 0 or the cell is an infra failure.
# setup = "setup.sh"

# Optional: shallow-clone depth for URL repos only.
# depth = 1
"""


def write_git_workspace_toml(
    task_dir: str,
    ref: str,
    *,
    repo: str = ".",
    subdir: str | None = None,
) -> str:
    """Write a ``workspace.toml`` template into ``task_dir``. Returns its path."""
    if not isinstance(ref, str) or not ref.strip():
        raise WorkspaceError("git ref must be a non-empty string")
    subdir_line = (
        f'subdir = "{_normalize_subdir(subdir)}"'
        if subdir
        else "# subdir = \"path/inside/repo\""
    )
    path = workspace_toml_path(task_dir)
    body = GIT_WORKSPACE_TOML_TEMPLATE.format(
        ref=ref.strip(),
        subdir_line=subdir_line,
    )
    # Keep repo editable in the template when non-default.
    if repo != ".":
        body = body.replace('repo = "."', f'repo = "{repo}"', 1)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path
