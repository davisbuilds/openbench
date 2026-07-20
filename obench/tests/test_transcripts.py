#!/usr/bin/env python3
"""Tests for LOCAL-ONLY per-cell transcript persistence in run.py.

Drives run.py end-to-end over the write-marker fixture and asserts each cell's
transcript is written, prefers the adapter's ``full_output`` over
``output_tail``, falls back to ``output_tail`` when ``full_output`` is absent,
and lands in the default (results-sibling) location when unspecified.
"""

import os

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import subprocess
import sys
import tempfile
import unittest

FIXTURES_DIR = os.path.join(BENCH_DIR, "tests", "fixtures")
RUN_MOD = ["-m", "obench.run"]
from obench import run  # noqa: E402


class TestTranscripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_transcripts_")
        self.results_path = os.path.join(self.tmp, "results.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, harness, transcripts_dir=None):
        argv = [sys.executable, *RUN_MOD,
                "--task", "write-marker",
                "--harness", harness,
                "--model", "gpt-5.5-medium",
                "--results-path", self.results_path,
                "--adapters-dir", FIXTURES_DIR,
                "--tasks-dir", FIXTURES_DIR]
        if transcripts_dir is not None:
            argv += ["--transcripts-dir", transcripts_dir]
        proc = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         msg=f"run.py failed:\n{proc.stdout}\n{proc.stderr}")
        return proc

    def test_transcript_written_per_cell_prefers_full_output(self):
        tdir = os.path.join(self.tmp, "transcripts")
        self._run("fake_adapter", transcripts_dir=tdir)

        run_id = "fake_adapter:write-marker:gpt-5.5-medium:trial1"
        path = run.transcript_path(tdir, "results", run_id)
        self.assertTrue(os.path.isfile(path),
                        f"expected a transcript at {path}")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        # full_output is preferred over output_tail.
        self.assertIn("FULL_TRANSCRIPT_BEGIN", text)
        self.assertIn("FULL_TRANSCRIPT_END", text)
        # Header carries provenance + the local-only warning.
        self.assertIn(run_id, text)
        self.assertIn("LOCAL-ONLY", text)

    def test_transcript_falls_back_to_output_tail(self):
        # The null adapter reports no full_output and an empty output_tail; the
        # transcript is still written (header only, empty body).
        tdir = os.path.join(self.tmp, "transcripts")
        self._run("null", transcripts_dir=tdir)

        run_id = "null:write-marker:gpt-5.5-medium:trial1"
        path = run.transcript_path(tdir, "results", run_id)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(run_id, text)
        self.assertNotIn("FULL_TRANSCRIPT_BEGIN", text)

    def test_default_location_is_results_sibling(self):
        # With no --transcripts-dir, transcripts land in a transcripts/ sibling
        # of the results log, under a subdir named for the results-file stem.
        self._run("fake_adapter")
        run_id = "fake_adapter:write-marker:gpt-5.5-medium:trial1"
        expected = run.transcript_path(
            os.path.join(self.tmp, "transcripts"), "results", run_id)
        self.assertTrue(os.path.isfile(expected),
                        f"expected default transcript at {expected}")

    def test_filename_sanitizes_run_id(self):
        # run_id colons must become filesystem-safe; the path must not contain
        # a raw ':' separator in its basename.
        run_id = "codex:some-task:gpt-5.5-medium:trial3"
        path = run.transcript_path("/tmp/base", "results", run_id)
        self.assertNotIn(":", os.path.basename(path))
        self.assertTrue(path.endswith(
            "codex_some-task_gpt-5.5-medium_trial3.txt"))


if __name__ == "__main__":
    unittest.main()
