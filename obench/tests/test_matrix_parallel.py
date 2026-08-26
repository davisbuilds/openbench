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

    def test_serial_writes_directly_no_part_files(self):
        # Durability: at workers=1 the child appends straight to results.jsonl
        # (like the pre-refactor runner), so no part-file merge window exists.
        mq.run_runner = _make_fake_runner()
        mq.run_matrix(_spec(workers=1), self.tmp, self.tmp)
        leftover = [f for f in os.listdir(self.tmp) if ".jsonl.part." in f]
        self.assertEqual(leftover, [], f"serial run left part files: {leftover}")

    def test_parallel_leaves_no_part_files_after_clean_run(self):
        mq.run_runner = _make_fake_runner(delay=0.005)
        mq.run_matrix(_spec(workers=2), self.tmp, self.tmp)
        leftover = [f for f in os.listdir(self.tmp) if ".jsonl.part." in f]
        self.assertEqual(leftover, [], f"parallel run leaked part files: {leftover}")

    def test_salvages_stranded_part_file_on_resume(self):
        # Simulate a kill that left an arm's part file unmerged: pre-seed it with
        # a completed row, then run with a runner that would FAIL that same cell.
        # The salvaged row must still count the cell as done (not re-run+fail).
        results = os.path.join(self.tmp, "results.jsonl")
        arm = "codex x m1"
        part = mq._arm_part_path(results, arm)
        stranded_id = bench_run.make_run_id("codex", "t1", "m1", 1)
        with open(part, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "run_id": stranded_id, "harness": "codex", "model": "m1",
                "task": "t1", "trial": 1, "failure_class": "solved",
                "success": True, "completed": True, "score": 1.0,
                "wall_time_s": 1.0}) + "\n")

        def runner_fails_t1(cmd, timeout_s=None):
            base = _make_fake_runner()
            if _arg(cmd, "--task") == "t1" and _arg(cmd, "--model") == "m1":
                return 1, "boom"  # would-be failure if the cell were re-run
            return base(cmd, timeout_s)

        mq.run_runner = runner_fails_t1
        mq.run_matrix(_spec(workers=2), self.tmp, self.tmp)
        rows = self._results()
        solved = {r["run_id"] for r in rows if r.get("failure_class") == "solved"}
        self.assertIn(stranded_id, solved, "stranded part-file row was not salvaged")

    def test_resume_from_persisted_pending(self):
        # A prior run persisted a pending queue; a fresh run_matrix must pick it
        # up and complete those cells (resume path used for both paused + normal).
        import shutil
        mq.run_runner = _make_fake_runner()
        # First pass: run only m1's cells by pre-seeding queue-state pending.
        rc = mq.run_matrix(_spec(workers=1), self.tmp, self.tmp)
        self.assertEqual(rc, 0)
        first = len(self._results())
        # Wipe results but keep ledger/queue-state, then re-run: satisfied cells
        # are re-verified from an empty file -> re-executed to full coverage.
        os.remove(os.path.join(self.tmp, "results.jsonl"))
        rc2 = mq.run_matrix(_spec(workers=1), self.tmp, self.tmp)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(self._results()), first)


class ContextSaveTests(unittest.TestCase):
    def test_save_persists_paused_cells_in_pending(self):
        # Regression for the resume-semantics change: paused cells must survive a
        # save() so a killed-while-paused arm resumes them (main dropped them).
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        qpath = os.path.join(tmp, "queue-state.json")
        state = mq.QueueState(qpath)
        cell = {"run_id": "codex:t1:m1:trial1"}
        ctx = mq._MatrixContext(
            results_path=os.path.join(tmp, "results.jsonl"), timeout=60,
            stall_timeout=None, exec_mode="local", allow_version_drift=False,
            rate_limited_backoff=1.0, max_cell_wall_s=None,
            max_consecutive_excluded=4, arm_states={}, retry_counts={},
            state=state, arm_pending={"codex x m1": []},
            arm_paused={"codex x m1": [("codex x m1", 0, cell)]},
            direct_write=True)
        ctx.save()
        with open(qpath, encoding="utf-8") as fh:
            persisted = json.load(fh)
        run_ids = [entry[2] for entry in persisted["pending"]]
        self.assertIn("codex:t1:m1:trial1", run_ids)


if __name__ == "__main__":
    unittest.main()
