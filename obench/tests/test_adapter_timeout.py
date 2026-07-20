#!/usr/bin/env python3
"""Regression tests for the adapters' TimeoutExpired handling.

On Python 3.14 `subprocess.run(text=True, timeout=...)` raises TimeoutExpired
whose `.stdout`/`.stderr` are BYTES, not str. The old handler did
`(e.stdout or "") + (e.stderr or "")`, which raises TypeError when one side is
bytes and the other is the ``""`` fallback (bytes + str) -> the adapter crashed
instead of returning a clean timeout row (this corrupted M4 cells). These tests
lock in the decode-safe `_err_tail` helper and the clean-timeout-dict contract.
"""

import importlib.util
import os

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import subprocess
import sys
import tempfile
import unittest

ADAPTERS_DIR = os.path.join(BENCH_DIR, "adapters")
ADAPTERS = ["codex", "opencode", "pi", "cursor", "devin"]


def load(name):
    spec = importlib.util.spec_from_file_location(f"to_{name}",
                                                  os.path.join(ADAPTERS_DIR, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestErrTailDecode(unittest.TestCase):
    """_err_tail must handle bytes/str/None in any combination, never raising."""

    def test_all_adapters_decode_safely(self):
        for name in ADAPTERS:
            mod = load(name)
            with self.subTest(adapter=name):
                # The real crash case: bytes stdout, None stderr.
                e = subprocess.TimeoutExpired("cmd", 1, output=b"out-bytes", stderr=None)
                tail = mod._err_tail(e)
                self.assertIsInstance(tail, str)
                self.assertIn("out-bytes", tail)
                # None stdout, bytes stderr.
                e2 = subprocess.TimeoutExpired("cmd", 1, output=None, stderr=b"err-bytes")
                self.assertIn("err-bytes", mod._err_tail(e2))
                # Both None.
                self.assertEqual(mod._err_tail(subprocess.TimeoutExpired("cmd", 1)), "")
                # Both str (text-mode success path still works).
                e4 = subprocess.TimeoutExpired("cmd", 1, output="s-out", stderr="s-err")
                self.assertEqual(mod._err_tail(e4), "s-outs-err")

    def test_limit_truncates(self):
        mod = load("opencode")
        e = subprocess.TimeoutExpired("cmd", 1, output=b"x" * 5000, stderr=None)
        self.assertEqual(len(mod._err_tail(e)), 2000)


class TestTimeoutReturnsCleanDict(unittest.TestCase):
    """A TimeoutExpired (with bytes output) must yield a clean timeout row."""

    def _run_with_timeout(self, mod, model):
        orig = mod.subprocess.run

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("cmd", 1, output=b"partial-bytes", stderr=None)

        mod.subprocess.run = boom
        workdir = tempfile.mkdtemp(prefix="to_test_")
        try:
            return mod.run("do the task", workdir, model, 1)
        finally:
            mod.subprocess.run = orig

    def _assert_clean(self, result):
        self.assertFalse(result["completed"])
        self.assertIsNotNone(result["error"])
        self.assertIn("timeout", result["error"].lower())
        self.assertIsInstance(result["output_tail"], str)
        self.assertIn("partial-bytes", result["output_tail"])
        self.assertIsNone(result["tokens"])
        self.assertIsNone(result["turns"])

    # Adapters whose run() has no pre-subprocess filesystem/auth dependency for
    # the canonical closed model.
    def test_opencode(self):
        self._assert_clean(self._run_with_timeout(load("opencode"), "gpt-5.5-medium"))

    def test_codex(self):
        self._assert_clean(self._run_with_timeout(load("codex"), "gpt-5.5-medium"))

    def test_cursor(self):
        self._assert_clean(self._run_with_timeout(load("cursor"), "gpt-5.5-medium"))

    def test_devin(self):
        self._assert_clean(self._run_with_timeout(load("devin"), "gpt-5.5-medium"))
if __name__ == "__main__":
    unittest.main()
