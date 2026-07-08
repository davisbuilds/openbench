#!/usr/bin/env python3
"""Tests for failure_class JSONL backfill."""

import json
import os
import sys
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH_DIR)

import backfill_failure_class  # noqa: E402


MOONSHOT_429 = (
    "APIError: HTTP 429 rate_limit: TPD rate limit, current 1502271, "
    "limit 1500000. Please retry later."
)


class TestBackfillFailureClass(unittest.TestCase):
    def test_backfill_writes_new_file_and_summarizes(self):
        tmp = tempfile.mkdtemp(prefix="bench_backfill_")
        try:
            src = os.path.join(tmp, "in.jsonl")
            dst = os.path.join(tmp, "out.jsonl")
            rows = [
                {"harness": "h", "model": "m", "task": "a", "success": True},
                {"harness": "h", "model": "m", "task": "b", "success": False,
                 "output_tail": MOONSHOT_429},
                {"harness": "h", "model": "m", "task": "c", "success": False,
                 "error": "No such image: openbench-harness:latest"},
                {"harness": "h", "model": "m", "task": "d", "success": False,
                 "wall_time_s": 1201, "tokens": None, "turns": None,
                 "output_tail": "", "error": "timeout after 1200s"},
                {"harness": "h", "model": "m", "task": "e", "success": False,
                 "wall_time_s": 1201, "tokens": 90000, "turns": 20,
                 "output_tail": "", "error": "timeout after 1200s"},
            ]
            with open(src, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            count, counts = backfill_failure_class.backfill(src, dst)

            self.assertEqual(count, 5)
            self.assertEqual(counts["solved"], 1)
            self.assertEqual(counts["rate_limited"], 1)
            self.assertEqual(counts["infra"], 2)
            self.assertEqual(counts["timeout"], 1)
            with open(dst, encoding="utf-8") as fh:
                out = [json.loads(line) for line in fh]
            self.assertEqual([r["failure_class"] for r in out],
                             ["solved", "rate_limited", "infra", "infra", "timeout"])
            summary = backfill_failure_class.format_summary(count, counts, dst)
            self.assertIn("tail-only detection is weaker", summary)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preserves_existing_valid_failure_class(self):
        tmp = tempfile.mkdtemp(prefix="bench_backfill_preserve_")
        try:
            src = os.path.join(tmp, "in.jsonl")
            dst = os.path.join(tmp, "out.jsonl")
            # Simulates a write-time-classified row whose provider marker only
            # existed in full_output and was intentionally not persisted.
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"success": False, "failure_class": "rate_limited"}) + "\n")
            count, counts = backfill_failure_class.backfill(src, dst)
            self.assertEqual(count, 1)
            self.assertEqual(counts["rate_limited"], 1)
            with open(dst, encoding="utf-8") as fh:
                self.assertEqual(json.loads(fh.readline())["failure_class"], "rate_limited")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_refuses_in_place_mutation(self):
        with self.assertRaises(SystemExit):
            backfill_failure_class.backfill("same.jsonl", "same.jsonl")

    def test_refuses_symlink_alias_to_input(self):
        tmp = tempfile.mkdtemp(prefix="bench_backfill_alias_")
        try:
            src = os.path.join(tmp, "in.jsonl")
            alias = os.path.join(tmp, "alias.jsonl")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"success": True}) + "\n")
            os.symlink(src, alias)
            with self.assertRaises(SystemExit):
                backfill_failure_class.backfill(src, alias)
            with open(src, encoding="utf-8") as fh:
                self.assertTrue(fh.read().strip(), "input must not be truncated")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
