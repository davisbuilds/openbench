"""Resolve default filesystem paths for repo vs installed usage.

When the current working directory contains a ``tasks/`` directory, defaults
match the historical OpenBench checkout layout. Otherwise tasks are discovered
under ``./tasks`` or ``./.openbench/tasks``, results default under CWD, and
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


def default_adapters_dir() -> str:
    """Packaged adapters directory (overridable via ``--adapters-dir``)."""
    return os.path.join(PACKAGE_DIR, "adapters")


def default_results_path(start: str | None = None) -> str:
    """Default results JSONL path under the repo root or CWD."""
    root = find_repo_root(start) or os.path.abspath(start or os.getcwd())
    return os.path.join(root, "results", "results.jsonl")


def default_tasks_dir(start: str | None = None) -> str | None:
    """Best-effort tasks directory, or ``None`` if nothing exists."""
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
    return None


def resolve_tasks_dir(explicit: str | None = None, start: str | None = None) -> str:
    """Return a tasks directory path or raise :class:`TasksDirError`.

    An explicit ``--tasks-dir`` is returned as an absolute path without requiring
    that it already exist (the runner reports missing tasks later). Auto-
    discovery requires an existing directory.
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
        "Run from an OpenBench checkout, create ./tasks or ./.openbench/tasks, "
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
