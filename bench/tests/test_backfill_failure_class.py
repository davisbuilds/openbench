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
            ]
            with open(src, "w", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")

            count, counts = backfill_failure_class.backfill(src, dst)

            self.assertEqual(count, 3)
            self.assertEqual(counts["solved"], 1)
            self.assertEqual(counts["rate_limited"], 1)
            self.assertEqual(counts["infra"], 1)
            with open(dst, encoding="utf-8") as fh:
                out = [json.loads(line) for line in fh]
            self.assertEqual([r["failure_class"] for r in out],
                             ["solved", "rate_limited", "infra"])
            summary = backfill_failure_class.format_summary(count, counts, dst)
            self.assertIn("tail-only detection is weaker", summary)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_refuses_in_place_mutation(self):
        with self.assertRaises(SystemExit):
            backfill_failure_class.backfill("same.jsonl", "same.jsonl")


if __name__ == "__main__":
    unittest.main()
