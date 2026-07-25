#!/usr/bin/env python3
"""The task image digest must have ONE source of truth.

Incident (2026-07-23/24): the digest was written in both ``task.toml`` (read by
the runner) and ``checker.sh`` (read by the checker). Rebuilding an image
changed the digest; updating only ``task.toml`` left the checker pointing at a
digest that no longer existed, so docker refused to start it, exited 125, and
the cell was recorded as the MODEL answering wrongly. 13 cells were corrupted
that way across three arms, and it recurred on every rebuild.

The runner now exports ``BENCH_TASK_IMAGE`` and checkers consume it, so the
agent and its checker are guaranteed to share an image and a rebuild has
nothing to desync.
"""
import glob
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PINNED_LITERAL = re.compile(r'^IMAGE=[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}\s*$', re.M)
CONSUMES_ENV = re.compile(r'BENCH_TASK_IMAGE')


def _pinned_checkers():
    pattern = os.path.join(REPO, "data", "packs", "*", "*", "checker.sh")
    return [p for p in sorted(glob.glob(pattern))
            if "sha256:" in open(p, encoding="utf-8").read()]


class TaskImageSingleSourceTests(unittest.TestCase):
    def test_no_checker_hardcodes_the_image_as_its_only_source(self):
        offenders = []
        for checker in _pinned_checkers():
            src = open(checker, encoding="utf-8").read()
            if PINNED_LITERAL.search(src) and not CONSUMES_ENV.search(src):
                offenders.append(os.path.relpath(checker, REPO))
        self.assertEqual(
            offenders, [],
            "these checkers pin a digest without honoring BENCH_TASK_IMAGE, so an "
            "image rebuild will desync them from task.toml and crash the checker "
            "(exit 125), which scores as a model failure: " + ", ".join(offenders))

    def test_pinned_checkers_read_the_runner_supplied_image(self):
        checkers = _pinned_checkers()
        self.assertTrue(checkers, "expected at least one pinned-image checker to guard")
        for checker in checkers:
            with self.subTest(checker=os.path.relpath(checker, REPO)):
                src = open(checker, encoding="utf-8").read()
                self.assertIn("BENCH_TASK_IMAGE", src)

    def test_runner_exports_the_image_to_the_checker(self):
        from obench import run as bench_run
        import inspect
        src = inspect.getsource(bench_run.run_checker)
        self.assertIn("BENCH_TASK_IMAGE", src,
                      "run_checker must export the resolved task image")
        sig = inspect.signature(bench_run.run_checker)
        self.assertIn("task_image", sig.parameters)


if __name__ == "__main__":
    unittest.main()
