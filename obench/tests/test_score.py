#!/usr/bin/env python3
"""Unit + e2e tests for the SCORE contract and version stamping (task #10)."""

import json
import os

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import subprocess
import sys
import types
import tempfile
import unittest

FIXTURES_DIR = os.path.join(BENCH_DIR, "tests", "fixtures")
RUN_PY = os.path.join(BENCH_DIR, "run.py")
from obench import run  # noqa: E402
from obench import report  # noqa: E402


class TestParseScore(unittest.TestCase):
    def test_absent(self):
        self.assertIsNone(run.parse_score("all good\nno score here"))
        self.assertIsNone(run.parse_score(""))
        self.assertIsNone(run.parse_score(None))

    def test_present(self):
        self.assertEqual(run.parse_score("SCORE: 0.5"), 0.5)
        self.assertEqual(run.parse_score("noise\nSCORE:0.75\nmore"), 0.75)

    def test_last_parseable_wins(self):
        self.assertEqual(run.parse_score("SCORE: 0.1\nSCORE: 0.9"), 0.9)

    def test_malformed_ignored_falls_back_to_earlier(self):
        # A trailing garbage SCORE line must not erase an earlier valid one.
        self.assertEqual(run.parse_score("SCORE: 0.4\nSCORE: oops"), 0.4)
        self.assertIsNone(run.parse_score("SCORE: nan?\nSCORE: xyz"))

    def test_clamped(self):
        self.assertEqual(run.parse_score("SCORE: 1.5"), 1.0)
        self.assertEqual(run.parse_score("SCORE: -3"), 0.0)


class TestVersionProbe(unittest.TestCase):
    def test_extract_from_module(self):
        m = types.ModuleType("m"); m.version = lambda: "v9"
        self.assertEqual(run._extract_version(m), "v9")

    def test_extract_missing(self):
        self.assertIsNone(run._extract_version(types.ModuleType("m")))

    def test_extract_non_string(self):
        m = types.ModuleType("m"); m.version = lambda: 123
        self.assertIsNone(run._extract_version(m))

    def test_extract_raises(self):
        def boom(): raise RuntimeError("x")
        m = types.ModuleType("m"); m.version = boom
        self.assertIsNone(run._extract_version(m))

    def test_null_is_builtin(self):
        self.assertEqual(run.probe_version("null", FIXTURES_DIR), "builtin")

    def test_probe_loads_adapter(self):
        self.assertEqual(run.probe_version("versioned_adapter", FIXTURES_DIR), "vfake-1.2.3")
        self.assertIsNone(run.probe_version("fake_adapter", FIXTURES_DIR))


class TestScoreCoercionE2E(unittest.TestCase):
    """Drive run.py end-to-end over the fixtures via subprocess."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_score_")
        self.results = os.path.join(self.tmp, "r.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, task, harness):
        proc = subprocess.run(
            [sys.executable, RUN_PY, "--task", task, "--harness", harness,
             "--results-path", self.results, "--adapters-dir", FIXTURES_DIR,
             "--tasks-dir", FIXTURES_DIR],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        with open(self.results) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def _row(self, task, harness):
        rid = f"{harness}:{task}:gpt-5.5-medium:trial1"
        return next(r for r in self._run(task, harness) if r["run_id"] == rid)

    def test_partial_credit(self):
        r = self._row("partial-credit", "fake_adapter")
        self.assertFalse(r["success"])       # nonzero exit
        self.assertEqual(r["checker_exit"], 1)
        self.assertEqual(r["score"], 0.5)    # last parseable SCORE line

    def test_exit0_coerces_score_to_one(self):
        r = self._row("full-despite-score", "fake_adapter")
        self.assertTrue(r["success"])
        self.assertEqual(r["checker_exit"], 0)
        self.assertEqual(r["score"], 1.0)    # SCORE: 0.3 ignored on a pass

    def test_null_partial_still_scores_from_checker(self):
        # The checker owns the score even when the adapter does nothing.
        r = self._row("partial-credit", "null")
        self.assertFalse(r["success"])
        self.assertEqual(r["score"], 0.5)
        self.assertEqual(r["harness_version"], "builtin")

    def test_version_stamped(self):
        r = self._row("write-marker", "versioned_adapter")
        self.assertEqual(r["harness_version"], "vfake-1.2.3")
        self.assertEqual(r["score"], 1.0)    # solves it -> exit 0

    def test_version_none_when_absent(self):
        r = self._row("write-marker", "fake_adapter")
        self.assertIsNone(r["harness_version"])


class TestRunCheckerTimeoutScore(unittest.TestCase):
    def test_timeout_scores_zero(self):
        # slow-checker fixture sleeps 30s; a 1s checker-timeout -> score 0.0.
        tmp = tempfile.mkdtemp()
        results = os.path.join(tmp, "r.jsonl")
        proc = subprocess.run(
            [sys.executable, RUN_PY, "--task", "slow-checker", "--harness", "null",
             "--checker-timeout", "1", "--results-path", results,
             "--adapters-dir", FIXTURES_DIR, "--tasks-dir", FIXTURES_DIR],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        with open(results) as fh:
            row = [json.loads(l) for l in fh if l.strip()][0]
        self.assertEqual(row["checker_exit"], "timeout")
        self.assertEqual(row["score"], 0.0)
        import shutil; shutil.rmtree(tmp, ignore_errors=True)


class TestReportMeanScore(unittest.TestCase):
    def _rows(self, specs):
        out = []
        for i, (h, succ, score) in enumerate(specs, 1):
            row = {"harness": h, "task": "t", "trial": i, "success": succ}
            if score is not None:
                row["score"] = score
            out.append(row)
        return out

    def test_mean_score_aggregation(self):
        # A has explicit partial scores; averaged over ALL trials incl. failures.
        rows = self._rows([("A", False, 0.5), ("A", True, 1.0), ("A", False, 0.0)])
        _, _, stats = report.aggregate(rows)
        self.assertAlmostEqual(report.mean(stats["A"]["scores"]), 0.5)  # (0.5+1+0)/3

    def test_old_rows_derive_score_from_success(self):
        # Rows lacking the field derive 1.0/0.0 from success (backward compat).
        rows = self._rows([("B", True, None), ("B", False, None)])
        _, _, stats = report.aggregate(rows)
        self.assertEqual(stats["B"]["scores"], [1.0, 0.0])

    def test_mixed_old_and_new(self):
        rows = self._rows([("C", True, None), ("C", False, 0.25)])
        _, _, stats = report.aggregate(rows)
        self.assertAlmostEqual(report.mean(stats["C"]["scores"]), 0.625)  # (1+0.25)/2

    def test_tables_include_mscore(self):
        rows = self._rows([("A", False, 0.5), ("A", True, 1.0)])
        harnesses, tasks, stats = report.aggregate(rows)
        main = report.format_table(harnesses, tasks, stats)
        eff = report.format_efficiency(harnesses, stats)
        self.assertIn("mscore", main)
        self.assertIn("mscore", eff)
        # mean score 0.75 shows in both
        self.assertIn("0.75", main)
        self.assertIn("0.75", eff)


if __name__ == "__main__":
    unittest.main()
