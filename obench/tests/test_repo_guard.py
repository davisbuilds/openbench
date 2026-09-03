#!/usr/bin/env python3
"""The test suite must not leave the developer's real checkout on another branch.

A few tests drive git against throwaway temp repos. If one ever runs a
branch-changing git command against the REAL repository root instead of its temp
dir, a green suite silently strands the developer on the wrong branch -- a side
effect that is hard to notice and hard to attribute. The package guard in
``obench/tests/__init__`` records the checkout's branch before any test runs and
restores it at interpreter exit if it moved. These tests exercise that logic
against a throwaway repo (never the real one).
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from obench import tests as tests_pkg


def _git(root, *args):
    subprocess.run(
        ["git", "-C", root, *args],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


class RepoGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="obench_repo_guard_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "T")
        _git(self.root, "checkout", "-q", "-b", "main")
        with open(os.path.join(self.root, "f"), "w") as fh:
            fh.write("x\n")
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "init")

    def test_current_branch_reads_the_checked_out_branch(self):
        self.assertEqual(tests_pkg._current_branch(self.root), "main")
        _git(self.root, "checkout", "-q", "-b", "feature")
        self.assertEqual(tests_pkg._current_branch(self.root), "feature")

    def test_current_branch_is_none_on_detached_head(self):
        sha = subprocess.check_output(
            ["git", "-C", self.root, "rev-parse", "HEAD"], text=True).strip()
        _git(self.root, "checkout", "-q", sha)
        self.assertIsNone(tests_pkg._current_branch(self.root))

    def test_current_branch_is_none_outside_a_repo(self):
        empty = tempfile.mkdtemp(prefix="obench_not_a_repo_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        self.assertIsNone(tests_pkg._current_branch(empty))

    def test_restore_returns_a_moved_branch_to_its_original(self):
        _git(self.root, "checkout", "-q", "-b", "wandered")
        self.assertEqual(tests_pkg._current_branch(self.root), "wandered")
        tests_pkg._restore_branch_if_moved(self.root, "main")
        self.assertEqual(tests_pkg._current_branch(self.root), "main")

    def test_restore_is_a_noop_when_branch_is_unchanged(self):
        # No exception, and the branch stays put.
        tests_pkg._restore_branch_if_moved(self.root, "main")
        self.assertEqual(tests_pkg._current_branch(self.root), "main")

    def test_restore_is_a_noop_when_no_initial_branch_recorded(self):
        # A detached / non-repo start records None; restore must do nothing.
        _git(self.root, "checkout", "-q", "-b", "somewhere")
        tests_pkg._restore_branch_if_moved(self.root, None)
        self.assertEqual(tests_pkg._current_branch(self.root), "somewhere")


if __name__ == "__main__":
    unittest.main()
