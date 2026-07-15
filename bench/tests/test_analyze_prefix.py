#!/usr/bin/env python3
"""Deterministic tests for proxy-ledger prefix analysis."""

import json
import os
import subprocess
import sys
import unittest

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(BENCH, "tests", "fixtures")
sys.path.insert(0, BENCH)

import analyze_prefix  # noqa: E402


class PrefixAnalysisTests(unittest.TestCase):
    def _analyze(self, name):
        return analyze_prefix.analyze(
            analyze_prefix.read_ledger(os.path.join(FIXTURES, name)))

    def test_single_session_has_no_cross_session_duplication(self):
        result = self._analyze("prefix_single_session.jsonl")
        self.assertEqual(result["sessions_observed"], 1)
        self.assertEqual(result["total_input_tokens"], 250)
        self.assertEqual(result["duplicated_prefix_tokens_estimate"], 0)
        self.assertEqual(result["first_request_input_tokens_by_session"], [100])

    def test_multi_session_estimates_first_request_overlap(self):
        result = self._analyze("prefix_multi_session.jsonl")
        self.assertEqual(result["sessions_observed"], 2)
        self.assertEqual(result["total_input_tokens"], 460)
        self.assertEqual(result["duplicated_prefix_tokens_estimate"], 90)
        self.assertEqual(result["first_request_input_tokens_by_session"], [100, 90])

    def test_missing_conversation_links_is_reported_unknown(self):
        result = analyze_prefix.analyze([
            {"usage": {"prompt_tokens": 20, "completion_tokens": 1}},
        ])
        self.assertIsNone(result["sessions_observed"])
        self.assertIsNone(result["duplicated_prefix_tokens_estimate"])
        self.assertEqual(result["unidentified_calls"], 1)

    def test_cli_emits_json(self):
        path = os.path.join(FIXTURES, "prefix_single_session.jsonl")
        proc = subprocess.run(
            [sys.executable, os.path.join(BENCH, "analyze_prefix.py"), path],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(proc.stdout)["sessions_observed"], 1)


if __name__ == "__main__":
    unittest.main()
