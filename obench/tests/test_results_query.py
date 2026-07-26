#!/usr/bin/env python3
"""Tests for the results-query CLI's definitions.

These definitions replaced per-question throwaway scripts that twice produced
wrong numbers. Each test pins one of the definitions those scripts got wrong.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from obench import results_query as rq


def _write(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def _row(harness, model, task, trial, fc, success=False, ts=None):
    return {"harness": harness, "model": model, "task": task, "trial": trial,
            "run_id": f"{harness}:{task}:{model}:trial{trial}",
            "failure_class": fc, "success": success, "ts_iso": ts}


def _run(command, path, *extra):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rq.main([command, path, *extra])
    return buf.getvalue()


class ResultsQueryTests(unittest.TestCase):
    def test_retry_rows_count_cell_once_and_judged_beats_excluded(self):
        rows = [_row("pi", "m", "t", 1, "rate_limited"),
                _row("pi", "m", "t", 1, "solved", True)]
        out = _run("summary", _write(rows))
        # 1 solved, 1 judged, 1 planned, 100% coverage -- not 2 rows.
        self.assertRegex(out, r"pi x m\s+1\s+1\s+1\s+100%\s+100%")

    def test_force_rerun_latest_verdict_wins_regardless_of_file_order(self):
        # Two judged attempts at the same cell (a --force rerun after a
        # wrong_answer). The published rate must NOT depend on which line comes
        # first in the file: the chronologically-later attempt supersedes.
        early = _row("pi", "m", "t", 1, "wrong_answer", False, ts="2026-01-01T00:00")
        late = _row("pi", "m", "t", 1, "solved", True, ts="2026-01-02T00:00")
        for order in ([early, late], [late, early]):
            out = _run("summary", _write(order))
            line = [l for l in out.splitlines() if l.startswith("pi x m")][0]
            # latest attempt (solved) wins both ways: 1 solved / 1 judged.
            self.assertRegex(line, r"pi x m\s+1\s+1\s+1\s+100%")

    def test_judged_beats_excluded_even_when_excluded_is_later(self):
        # A real verdict must not be erased by a later infra-family rerun.
        verdict = _row("pi", "m", "t", 1, "wrong_answer", False, ts="2026-01-01T00:00")
        later_infra = _row("pi", "m", "t", 1, "rate_limited", False, ts="2026-01-09T00:00")
        out = _run("summary", _write([verdict, later_infra]))
        line = [l for l in out.splitlines() if l.startswith("pi x m")][0]
        # judged=1 (the wrong_answer), planned=1, 100% coverage.
        self.assertRegex(line, r"pi x m\s+0\s+1\s+1\s+0%\s+100%")

    def test_low_coverage_warns_and_names_missing_cells(self):
        rows = [_row("pi", "m", "t", 1, "solved", True),
                _row("pi", "m", "t", 2, "rate_limited")]
        out = _run("summary", _write(rows))
        self.assertIn("COVERAGE WARNING", out)
        self.assertIn("t#t2", out)

    def test_matched_uses_cells_not_tasks(self):
        # Arm A has verdicts on trials 1-2, arm B only on trial 1: matched must
        # be ONE cell, not "the task". The hand-rolled version of this
        # comparison aggregated per-task and mixed unmatched trials.
        rows = [_row("pi", "a", "t", 1, "solved", True),
                _row("pi", "a", "t", 2, "solved", True),
                _row("pi", "b", "t", 1, "wrong_answer"),
                _row("pi", "b", "t", 2, "rate_limited")]
        out = _run("matched", _write(rows))
        self.assertIn("matched cells (every arm has a verdict): 1", out)

    def test_errors_separates_model_failures_from_infra(self):
        rows = [_row("pi", "m", "t", 1, "wrong_answer"),
                _row("pi", "m", "t", 2, "timeout"),
                _row("pi", "m", "t", 3, "infra")]
        out = _run("errors", _write(rows))
        self.assertIn("real model failures: 2", out)
        self.assertIn("infra-family: 1", out)

    def test_mixed_host_arms_are_flagged(self):
        # tb-mid really did run deepseek on the laptop (the only host with its
        # API key) and the other arms on the mini. Solve rates survive that;
        # wall-time comparisons do not, and nothing in the numbers reveals it.
        rows = [_row("pi", "a", "t", 1, "solved", True),
                _row("pi", "b", "t", 1, "solved", True)]
        rows[0]["host"] = "laptop"
        rows[1]["host"] = "mini"
        out = _run("summary", _write(rows))
        self.assertIn("MIXED-HOST WARNING", out)
        self.assertIn("NOT wall time", out)

    def test_single_host_is_not_flagged(self):
        rows = [_row("pi", "a", "t", 1, "solved", True),
                _row("pi", "b", "t", 1, "solved", True)]
        for r in rows:
            r["host"] = "mini"
        self.assertNotIn("MIXED-HOST WARNING", _run("summary", _write(rows)))

    def test_rows_without_host_are_reported_as_unverifiable(self):
        # Every row predating the `host` field falls in here; silence would
        # imply single-host provenance we cannot actually confirm.
        out = _run("summary", _write([_row("pi", "a", "t", 1, "solved", True)]))
        self.assertIn("HOST-UNKNOWN", out)

    def test_evidence_prints_source_line(self):
        path = _write([_row("pi", "m", "t", 1, "solved", True)])
        out = _run("evidence", path, "--run-id", "pi:t:m")
        self.assertIn(f"{path}:1", out)
        self.assertIn("failure_class = solved", out)


if __name__ == "__main__":
    unittest.main()
