#!/usr/bin/env python3
"""The core task tier must stay portable and upstream-clean.

``tasks/`` is the upstream-owned core tier; keeping it byte-clean is what lets a
feature be promoted upstream without stripping fork-local cruft (see
docs/project/FORK_WORKFLOW.md). A checker that hard-codes a developer's absolute
path or a fork-local dependency env var passes on the machine that has it and
fails everywhere else -- exactly the trap that put a fork-local, agentmonitor-
dependent task into core and broke CI. Fork-local tasks belong in the
``tasks-local/`` tier, which is env-gated (exit-77 SKIP) instead.

This guard scans the real core tier, so it fails the moment such a task lands.
"""

import glob
import os
import unittest

from obench.paths import SOURCE_ROOT

_CORE_TASKS_DIR = os.path.join(SOURCE_ROOT, "tasks")

# Substrings that mark a checker as non-portable / fork-local. Absolute home
# paths bind a checker to one machine; the agentmonitor deps var is a fork-local
# dependency that has no place in the shared core tier.
_FORBIDDEN = (
    "/Users/",
    "/home/",
    "AGENTMONITOR_DEPS",
)


class CoreTaskPortabilityTests(unittest.TestCase):
    @unittest.skipUnless(
        os.path.isdir(_CORE_TASKS_DIR),
        "core tasks/ tier only present in an editable checkout",
    )
    def test_no_core_checker_references_a_machine_specific_path(self):
        offenders = []
        pattern = os.path.join(_CORE_TASKS_DIR, "*", "checker.sh")
        checkers = sorted(glob.glob(pattern))
        # Sanity: the guard is pointed at a populated tier, not an empty glob
        # (an absence that silently passes is worse than a failure).
        self.assertTrue(checkers, f"no core checkers found under {_CORE_TASKS_DIR}")
        for checker in checkers:
            with open(checker, encoding="utf-8") as fh:
                text = fh.read()
            for needle in _FORBIDDEN:
                if needle in text:
                    offenders.append(
                        f"{os.path.relpath(checker, SOURCE_ROOT)} contains "
                        f"{needle!r}")
        self.assertEqual(
            offenders, [],
            "Core-tier checkers must be portable. Move a fork-local, "
            "machine-specific task to tasks-local/ (env-gated exit-77 SKIP) "
            "per docs/project/FORK_WORKFLOW.md:\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
