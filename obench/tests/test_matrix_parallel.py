#!/usr/bin/env python3
"""End-to-end integration tests for the matrix scheduler: serial vs arm-parallel.

These give run_matrix its first full-loop net (previously only its helpers were
covered). A fake runner stands in for the real cell subprocess -- it writes a
solved row to whatever ``--results-path`` the cell was handed (the arm-private
part file), exactly as the real runner would append to results.jsonl. That
exercises the part-file merge, the per-arm drain, and the ThreadPoolExecutor
fan-out, and asserts serial and parallel produce identical coverage with no lost
or interleaved rows.
"""

import json
import os
import tempfile
import threading
import time
import unittest

from obench import matrix_queue as mq
from obench import run as bench_run


def _spec(results_path="results.jsonl", ledger="ledger", trials=1, workers=1):
    return {
        "results_path": results_path,
        "ledger_dir": ledger,
        "timeout": 60,
        "exec_mode": "local",
        "trials": trials,
        "workers": workers,
        "arm": [
            {"harness": "codex", "model": "m1"},
            {"harness": "codex", "model": "m2"},
        ],
        "task_group": [{"tasks": ["t1", "t2", "t3"]}],
    }


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _make_fake_runner(delay=0.0, record_concurrency=None, lock=None):
    """Return a run_runner stand-in that writes a solved row to the cell's
    --results-path. ``record_concurrency`` (a list) captures peak overlap."""
    live = {"n": 0, "peak": 0}

    def fake(cmd, timeout_s=None):
        if record_concurrency is not None:
            with lock:
                live["n"] += 1
                live["peak"] = max(live["peak"], live["n"])
                record_concurrency.append(live["peak"])
        if delay:
            time.sleep(delay)
        harness = _arg(cmd, "--harness")
        model = _arg(cmd, "--model")
        task = _arg(cmd, "--task")
        trial = int(_arg(cmd, "--trial"))
        results_path = _arg(cmd, "--results-path")
        run_id = bench_run.make_run_id(harness, task, model, trial)
        row = {
            "run_id": run_id, "harness": harness, "model": model, "task": task,
            "trial": trial, "failure_class": "solved", "success": True,
            "completed": True, "score": 1.0, "wall_time_s": 1.0,
        }
        with open(results_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        if record_concurrency is not None:
            with lock:
                live["n"] -= 1
        return 0, ""

    return fake


class MatrixIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self._orig = mq.run_runner

    def tearDown(self):
        mq.run_runner = self._orig

    def _results(self):
        path = os.path.join(self.tmp, "results.jsonl")
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_serial_full_coverage(self):
        mq.run_runner = _make_fake_runner()
        rc = mq.run_matrix(_spec(workers=1), self.tmp, self.tmp)
        self.assertEqual(rc, 0)
        rows = self._results()
        self.assertEqual(len(rows), 6)  # 2 arms x 3 tasks x 1 trial
        self.assertEqual(len({r["run_id"] for r in rows}), 6)
        # every row is a clean single JSON object (no interleave/torn lines)
        self.assertTrue(all(r["failure_class"] == "solved" for r in rows))

    def test_parallel_matches_serial_coverage(self):
        mq.run_runner = _make_fake_runner()
        rc = mq.run_matrix(_spec(workers=2), self.tmp, self.tmp)
        self.assertEqual(rc, 0)
        rows = self._results()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({r["run_id"] for r in rows}), 6)

    def test_parallel_actually_overlaps_arms(self):
        seen, lock = [], threading.Lock()
        mq.run_runner = _make_fake_runner(delay=0.05, record_concurrency=seen, lock=lock)
        rc = mq.run_matrix(_spec(workers=2), self.tmp, self.tmp)
        self.assertEqual(rc, 0)
        # two arms on distinct workers must have run cells concurrently at least once
        self.assertGreaterEqual(max(seen), 2, "arms never overlapped -- not parallel")

    def test_no_torn_lines_under_concurrency(self):
        mq.run_runner = _make_fake_runner(delay=0.01)
        mq.run_matrix(_spec(workers=2, trials=2), self.tmp, self.tmp)
        # Re-read raw: every line must parse (a torn append would raise).
        path = os.path.join(self.tmp, "results.jsonl")
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    json.loads(line)  # raises on a torn/interleaved write


if __name__ == "__main__":
    unittest.main()
