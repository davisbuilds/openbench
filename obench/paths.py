"""Resolve default filesystem paths for repo vs installed usage.

When the current working directory contains a ``tasks/`` directory, defaults
match the historical OpenBench checkout layout. Otherwise tasks are discovered
under ``./tasks`` or ``./.openbench/tasks``, results default under CWD (or
``.openbench/results/`` when an ``openbench.toml`` config is present), and
adapters come from the installed package.
"""

from __future__ import annotations

import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
# Parent of the package directory. In an editable/source checkout this is the
# OpenBench repo root; for a plain wheel install it is not meaningful for tasks.
SOURCE_ROOT = os.path.dirname(PACKAGE_DIR)


class TasksDirError(FileNotFoundError):
    """Raised when no tasks directory can be resolved."""


def find_repo_root(start: str | None = None) -> str | None:
    """Return ``start`` (default: cwd) when it contains a ``tasks/`` directory."""
    cwd = os.path.abspath(start or os.getcwd())
    if os.path.isdir(os.path.join(cwd, "tasks")):
        return cwd
    return None


def docker_workdir_parent() -> str:
    """Parent dir for workspaces that get bind-mounted into a container.

    macOS ``tempfile`` defaults to ``/var/folders/...``, which Docker Desktop
    shares into its VM but **colima does not**: the bind mount then resolves to
    a directory the container sees as empty, so every checker test fails on
    missing files while the host workspace looks correct. Staging under the
    source tree keeps the path inside colima's shared mount.

    Found twice: the runner hit it first (hence ``OPENBENCH_DOCKER_TMPDIR``),
    then ``validate_tasks`` hit the same wall independently -- 5 of 6 tb-mid
    tasks failed their solution stage on the colima host and passed on the
    Docker Desktop host, from nothing but this path choice.
    """
    parent = os.environ.get("OPENBENCH_DOCKER_TMPDIR") or os.path.join(
        SOURCE_ROOT, ".bench-tmp")
    os.makedirs(parent, exist_ok=True)
    return parent


def default_adapters_dir() -> str:
    """Packaged adapters directory (overridable via ``--adapters-dir``)."""
    return os.path.join(PACKAGE_DIR, "adapters")


def default_results_path(start: str | None = None) -> str:
    """Default results JSONL path under the repo root, config, or CWD."""
    from .config import load_config

    cfg = load_config(start)
    if cfg.results_path:
        return cfg.results_path
    root = find_repo_root(start) or os.path.abspath(start or os.getcwd())
    if cfg.project_root and not find_repo_root(start):
        return os.path.join(cfg.project_root, ".openbench", "results", "results.jsonl")
    return os.path.join(root, "results", "results.jsonl")


def default_tasks_dir(start: str | None = None) -> str | None:
    """Best-effort tasks directory, or ``None`` if nothing exists."""
    from .config import load_config

    cfg = load_config(start)
    if cfg.tasks_dir and os.path.isdir(cfg.tasks_dir):
        return cfg.tasks_dir
    root = find_repo_root(start)
    if root is not None:
        return os.path.join(root, "tasks")
    cwd = os.path.abspath(start or os.getcwd())
    for candidate in (
        os.path.join(cwd, "tasks"),
        os.path.join(cwd, ".openbench", "tasks"),
    ):
        if os.path.isdir(candidate):
            return candidate
    if cfg.tasks_dir:
        return cfg.tasks_dir
    return None


def resolve_tasks_dir(explicit: str | None = None, start: str | None = None) -> str:
    """Return a tasks directory path or raise :class:`TasksDirError`.

    An explicit ``--tasks-dir`` is returned as an absolute path without requiring
    that it already exist (the runner reports missing tasks later). Auto-
    discovery requires an existing directory (or a config ``tasks_dir`` that
    may not exist yet — returned so callers can report a clearer missing-task
    error later).
    """
    if explicit:
        return os.path.abspath(explicit)
    found = default_tasks_dir(start)
    if found is not None:
        return found
    cwd = os.path.abspath(start or os.getcwd())
    raise TasksDirError(
        "No tasks directory found.\n"
        f"Looked for {os.path.join(cwd, 'tasks')} and "
        f"{os.path.join(cwd, '.openbench', 'tasks')}.\n"
        "Run `obench init`, create ./tasks or ./.openbench/tasks, "
        "or pass --tasks-dir."
    )


def default_imported_tasks_dir(start: str | None = None) -> str | None:
    """Optional ``tasks-imported`` sibling when running inside a checkout."""
    root = find_repo_root(start)
    if root is None:
        return None
    path = os.path.join(root, "tasks-imported")
    return path if os.path.isdir(path) else None


def ensure_package_path_on_sys_path() -> str:
    """Put ``PACKAGE_DIR`` on ``sys.path`` so file-path-loaded adapters can
    ``import auth_persist`` (and similar flat imports) the way Docker mounts do.
    """
    import sys

    if PACKAGE_DIR not in sys.path:
        sys.path.insert(0, PACKAGE_DIR)
    return PACKAGE_DIR
