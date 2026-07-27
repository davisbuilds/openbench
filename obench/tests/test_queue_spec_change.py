#!/usr/bin/env python3
"""Narrowing or widening a spec must not crash the queue's resume.

A saved queue-state.json holds the run_ids that were still pending. Editing the
spec afterwards -- dropping a task group to refocus a campaign, or adding one --
means those ids no longer line up with the planned cells.

The resume did ``next(c for c in all_cells if c["run_id"] == p[2])`` and died on
a bare ``StopIteration`` with no hint that the spec had changed. Hit live while
narrowing a 3-group re-run down to tb-mid: the traceback gave no clue, and the
obvious reading was that the queue itself was broken.
"""

import json
import os
import tempfile
import unittest


def _plan(pending_raw, all_cells):
    """The resume logic under test, mirroring run_matrix's reconciliation."""
    by_run_id = {c["run_id"]: c for c in all_cells}
    pending = [(p[0], p[1], by_run_id[p[2]]) for p in pending_raw if p[2] in by_run_id]
    known = {p[2] for p in pending_raw}
    pending += [(c["arm"], c["arm_idx"], c) for c in all_cells
                if c["run_id"] not in known]
    return pending


def _cell(rid):
    return {"run_id": rid, "arm": "pi x m", "arm_idx": 0}


class SpecChangeResumeTests(unittest.TestCase):
    def test_narrowed_spec_drops_stale_cells_without_raising(self):
        saved = [["pi x m", 0, "keep"], ["pi x m", 0, "gone"]]
        pending = _plan(saved, [_cell("keep")])
        self.assertEqual([p[2]["run_id"] for p in pending], ["keep"])

    def test_widened_spec_picks_up_new_cells(self):
        saved = [["pi x m", 0, "keep"]]
        pending = _plan(saved, [_cell("keep"), _cell("fresh")])
        self.assertEqual(sorted(p[2]["run_id"] for p in pending), ["fresh", "keep"])

    def test_unchanged_spec_is_untouched(self):
        saved = [["pi x m", 0, "a"], ["pi x m", 0, "b"]]
        pending = _plan(saved, [_cell("a"), _cell("b")])
        self.assertEqual([p[2]["run_id"] for p in pending], ["a", "b"])

    def test_the_old_logic_would_have_raised(self):
        # Negative control: the exact expression that crashed.
        all_cells = [_cell("keep")]
        with self.assertRaises(StopIteration):
            next(c for c in all_cells if c["run_id"] == "gone")

    def test_real_module_reconciles_rather_than_raising(self):
        from obench import matrix_queue as mq
        src = open(mq.__file__, encoding="utf-8").read()
        self.assertNotIn('next(c for c in all_cells if c["run_id"] == p[2])', src,
                         "the crashing resume expression is back in matrix_queue")
        self.assertIn("spec changed since the last run", src,
                      "resume no longer reports a changed spec")
