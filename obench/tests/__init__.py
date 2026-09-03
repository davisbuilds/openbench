"""obench test package.

Guard: protect the developer's real git checkout from suite side effects.

A few tests in this suite drive git against throwaway temporary repos. If one
ever runs a branch-changing git command against the REAL repository -- cwd or
``-C`` pointed at the checkout root instead of a temp dir -- a green run silently
strands the developer on the wrong branch, a side effect that is easy to miss
and hard to attribute. This records the checkout's branch when the test package
is first imported (before any test module loads) and, at interpreter exit,
restores it with a loud warning if a test moved it. Best-effort and a no-op
outside an editable git checkout (an installed package, or a detached CI
checkout, records no branch and registers nothing).
"""

from __future__ import annotations

import atexit
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _current_branch(repo_root) -> str | None:
    """Return the checked-out branch name, or None if detached / not a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root),
             "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    branch = out.stdout.strip()
    return branch or None  # empty stdout on a detached HEAD


def _restore_branch_if_moved(repo_root, initial_branch) -> None:
    """Put ``repo_root`` back on ``initial_branch`` if a test moved it away."""
    if not initial_branch:
        return
    now = _current_branch(repo_root)
    if now is None or now == initial_branch:
        return
    sys.stderr.write(
        f"\n*** obench.tests guard: a test moved the real checkout from "
        f"'{initial_branch}' to '{now}'. Restoring '{initial_branch}'. Some test "
        f"ran a branch-changing git command against the repo root instead of a "
        f"temp dir -- find it by running test modules one at a time and checking "
        f"`git branch --show-current` after each. ***\n"
    )
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "checkout", "--quiet", initial_branch],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        sys.stderr.write(
            f"*** obench.tests guard: could not restore '{initial_branch}': "
            f"{exc}. Run `git checkout {initial_branch}` yourself. ***\n"
        )


# Captured at import, before any test module runs, so it is the pristine
# pre-suite branch. Only a real editable checkout on a branch arms the guard.
_INITIAL_BRANCH = (
    _current_branch(_REPO_ROOT) if (_REPO_ROOT / ".git").exists() else None
)
if _INITIAL_BRANCH is not None:
    atexit.register(_restore_branch_if_moved, _REPO_ROOT, _INITIAL_BRANCH)
