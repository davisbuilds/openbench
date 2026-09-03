#!/usr/bin/env python3
"""Task-checker validator: fork-local tier + environment-gated SKIP.

Two boundary behaviors the fork needs on top of the upstream core/imported
tiers:

  * a ``local`` tier (``tasks-local/``) so fork-local tasks are discovered
    without polluting the upstream-owned ``tasks/`` core tier, and
  * an environment-gated SKIP: a checker that cannot run in this environment
    (a fork-local task whose external deps are absent, e.g. in CI) exits 77 and
    the validator reports SKIP -- not FAIL, and never a faked PASS -- so a green
    run honestly records what it could not exercise.
"""

import contextlib
import io
import os
import shutil
import stat
import tempfile
import unittest

from obench import paths
from obench import validate_tasks as vt


def _write(path, content, executable=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    if executable:
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


def _make_task(root, name, checker_body, *, solved=True):
    """Create a minimal snapshot-workspace task under ``root``.

    ``checker_body`` is the body of checker.sh. When ``solved`` the solution/
    overlay drops ``solved.txt`` so a polarity-correct checker fails on the bare
    workspace and passes on the solution.
    """
    task = os.path.join(root, name)
    _write(os.path.join(task, "instruction.md"), "do the thing\n")
    _write(os.path.join(task, "checker.sh"), checker_body, executable=True)
    _write(os.path.join(task, "workspace", ".keep"), "")
    if solved:
        _write(os.path.join(task, "solution", "solved.txt"), "ok\n")
    else:
        os.makedirs(os.path.join(task, "solution"), exist_ok=True)
    return task


# A polarity-correct checker: fails on the bare workspace, passes once the
# solution overlay drops solved.txt.
_POLARITY_OK = "#!/bin/sh\n[ -f solved.txt ] && exit 0 || exit 1\n"
# An environment-gated checker that cannot run here.
_SKIP_77 = "#!/bin/sh\necho 'SKIP: external deps absent' >&2\nexit 77\n"


class LocalTierDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_vt_tier_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "tasks"))  # marks the repo root

    def test_default_local_tasks_dir_returns_tasks_local_when_present(self):
        self.assertIsNone(paths.default_local_tasks_dir(start=self.tmp))
        local = os.path.join(self.tmp, "tasks-local")
        os.makedirs(local)
        self.assertEqual(paths.default_local_tasks_dir(start=self.tmp), local)

    def test_discover_tasks_walks_a_local_root(self):
        local = os.path.join(self.tmp, "tasks-local")
        _make_task(local, "am-thing", _POLARITY_OK)
        found = vt.discover_tasks([("local", local)])
        self.assertEqual(found, [("local", "am-thing", os.path.join(local, "am-thing"))])


class SkipContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="obench_vt_skip_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run(self, root):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = vt.main(["--tasks-dir", root])
        return rc, buf.getvalue()

    def test_exit_77_is_skipped_not_failed(self):
        _make_task(self.tmp, "needs-deps", _SKIP_77)
        rc, out = self._run(self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIP", out)
        self.assertIn("skipped", out.lower())

    def test_skip_never_runs_the_solution_overlay(self):
        # A skipped checker must not be reported as a passing solution; the row
        # is SKIP, with no solution score claimed.
        _make_task(self.tmp, "needs-deps", _SKIP_77)
        rc, out = self._run(self.tmp)
        self.assertEqual(rc, 0, out)
        # No passing-solution cell is claimed for a skipped task.
        self.assertNotIn("PASS(ok)", out)

    def test_skip_does_not_mask_a_real_failure(self):
        _make_task(self.tmp, "needs-deps", _SKIP_77)
        # Bad polarity: passes on the bare workspace -> a real FAIL.
        _make_task(self.tmp, "broken", "#!/bin/sh\nexit 0\n")
        rc, out = self._run(self.tmp)
        self.assertEqual(rc, 1, out)
        self.assertIn("SKIP", out)
        self.assertIn("FAIL", out)

    def test_polarity_correct_task_still_passes(self):
        _make_task(self.tmp, "good", _POLARITY_OK)
        rc, out = self._run(self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)
        self.assertNotIn("SKIP", out)


if __name__ == "__main__":
    unittest.main()
