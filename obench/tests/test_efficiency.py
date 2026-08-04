#!/usr/bin/env python3
"""Unit tests for report.py efficiency aggregations and formatting.

Focus: tokens/turns per solve, mean turns, mean_s CI half-width, and the
mixed-data contract — a harness reporting no token data must render ``-`` and
must never be treated as zero.
"""

import os
import sys
import tempfile
import json
import unittest


from obench import report  # noqa: E402


def rows_for(harness, specs, task="t", model="-"):
    """Build result rows: specs = list of (success, tokens, turns, wall)."""
    out = []
    for i, (succ, tok, turn, wall) in enumerate(specs, 1):
        out.append({"harness": harness, "model": model, "task": task, "trial": i,
                    "success": succ, "tokens": tok, "turns": turn,
                    "wall_time_s": wall})
    return out


def _arm(harness, model="-"):
    return (harness, model)


class TestAggregation(unittest.TestCase):
    def test_token_and_turn_vals_exclude_none(self):
        rows = rows_for("h", [(True, 500, 2, 10.0), (True, None, None, 12.0),
                              (True, 700, 4, 14.0)])
        _, _, stats = report.aggregate(rows)
        st = stats[_arm("h")]
        # None values are dropped, not stored as 0.
        self.assertEqual(st["token_vals"], [500, 700])
        self.assertEqual(st["turn_vals"], [2, 4])
        self.assertEqual(st["wall_times"], [10.0, 12.0, 14.0])
        self.assertEqual(st["succ"], 3)

    def test_booleans_not_counted_as_numbers(self):
        # success is a bool; it must never leak into token/turn aggregation.
        rows = rows_for("h", [(True, None, None, 1.0)])
        _, _, stats = report.aggregate(rows)
        self.assertEqual(stats[_arm("h")]["token_vals"], [])
        self.assertEqual(stats[_arm("h")]["turn_vals"], [])

    def test_aggregate_splits_models_under_same_harness(self):
        rows = (
            rows_for("h", [(True, 100, None, 1.0)], model="m1")
            + rows_for("h", [(False, 200, None, 1.0)], model="m2")
        )
        arms, _tasks, stats = report.aggregate(rows)
        self.assertEqual(arms, [_arm("h", "m1"), _arm("h", "m2")])
        self.assertEqual(stats[_arm("h", "m1")]["succ"], 1)
        self.assertEqual(stats[_arm("h", "m2")]["succ"], 0)
        self.assertEqual(stats[_arm("h", "m2")]["n"], 1)


class TestPerSolveMetrics(unittest.TestCase):
    def test_tokens_per_solve_basic(self):
        _, _, stats = report.aggregate(rows_for("h", [
            (True, 100, None, 1.0), (True, 200, None, 1.0), (True, 300, None, 1.0)]))
        self.assertEqual(report.tokens_per_solve(stats[_arm("h")]), 200.0)  # 600/3

    def test_tokens_per_solve_mixed_none(self):
        # Only non-null tokens summed; denominator is solves.
        _, _, stats = report.aggregate(rows_for("h", [
            (True, 500, None, 1.0), (True, None, None, 1.0), (True, 700, None, 1.0)]))
        self.assertAlmostEqual(report.tokens_per_solve(stats[_arm("h")]), 1200 / 3)

    def test_tokens_per_solve_no_data_is_none(self):
        _, _, stats = report.aggregate(rows_for("h", [
            (True, None, None, 1.0), (True, None, None, 1.0)]))
        self.assertIsNone(report.tokens_per_solve(stats[_arm("h")]))

    def test_tokens_per_solve_zero_solves_is_none(self):
        # Tokens reported but nothing solved -> cannot normalise per solve.
        _, _, stats = report.aggregate(rows_for("h", [
            (False, 900, None, 1.0), (False, 100, None, 1.0)]))
        self.assertIsNone(report.tokens_per_solve(stats[_arm("h")]))

    def test_turns_per_solve(self):
        _, _, stats = report.aggregate(rows_for("h", [
            (True, None, 3, 1.0), (True, None, 5, 1.0)]))
        self.assertEqual(report.turns_per_solve(stats[_arm("h")]), 4.0)  # 8/2


class TestSummaryStats(unittest.TestCase):
    def test_mean_none_for_empty(self):
        self.assertIsNone(report.mean([]))
        self.assertEqual(report.mean([2, 4]), 3.0)

    def test_ci_halfwidth_needs_two(self):
        self.assertIsNone(report.ci_halfwidth([]))
        self.assertIsNone(report.ci_halfwidth([5.0]))
        # sd of [10,20] = 7.071..., hw = 1.96*7.071/sqrt(2) ~= 9.8
        self.assertAlmostEqual(report.ci_halfwidth([10.0, 20.0]), 9.8, places=1)


class TestFormatters(unittest.TestCase):
    def test_fmt_tokens_compact(self):
        self.assertEqual(report._fmt_tokens(None), "-")
        self.assertEqual(report._fmt_tokens(200), "200")
        self.assertEqual(report._fmt_tokens(44402.3), "44.4k")

    def test_fmt_turns(self):
        self.assertEqual(report._fmt_turns(None), "-")
        self.assertEqual(report._fmt_turns(1), "1.0")


class TestTables(unittest.TestCase):
    def _mixed(self):
        # Harness A reports tokens+turns; harness B reports neither.
        rows = (rows_for("A", [(True, 1000, 2, 10.0), (True, 2000, 4, 20.0)], task="t1")
                + rows_for("B", [(True, None, None, 5.0), (True, None, None, 7.0)], task="t1"))
        return report.aggregate(rows)

    def test_success_table_headers_and_data(self):
        arms, tasks, stats = self._mixed()
        text = report.format_table(arms, tasks, stats)
        for h in ("harness", "model", "wilson95", "mean_s", "tok/slv", "turns"):
            self.assertIn(h, text)
        lines = {ln.split()[0]: ln for ln in text.splitlines() if ln[:1] in "AB"}
        # A: tokens/solve = 3000/2 = 1500 -> "1.5k"; mean turns = 3.0
        self.assertIn("1.5k", lines["A"])
        self.assertIn("3.0", lines["A"])
        # B: no token/turn data -> "-" in both slots, never "0"
        self.assertRegex(lines["B"], r"\bB\b.*-\s+-\s*$")

    def test_efficiency_table_mixed_and_ci(self):
        arms, _tasks, stats = self._mixed()
        text = report.format_efficiency(arms, stats)
        for h in ("harness", "model", "success", "rate", "wilson95", "mean_s",
                  "tok/slv", "turns/slv"):
            self.assertIn(h, text)
        rowA = next(l for l in text.splitlines() if l.startswith("A"))
        rowB = next(l for l in text.splitlines() if l.startswith("B"))
        self.assertIn("1.5k", rowA)          # tokens/solve
        self.assertIn("±", rowA)             # mean_s carries a CI half-width
        self.assertNotIn("k", rowB)          # B has no token figure
        self.assertTrue(rowB.rstrip().endswith("-"))  # turns/solve dash for B


class TestProxyTokenBasis(unittest.TestCase):
    def test_mismatch_keeps_correctness_and_latency_but_excludes_efficiency(self):
        rows = [{
            "harness": "pi", "model": "model-x", "task": "t", "trial": 1,
            "success": True, "wall_time_s": 2.0, "tokens": 200,
            "usage_evidence_grade": "harbor_reported_proxy_mismatch",
            "usage_ranking_eligible": False,
            "usage_ranking_exclusion_reason": "proxy_mismatch",
        }]
        arms, _tasks, aggregated = report.aggregate(rows)
        arm = aggregated[_arm("pi", "model-x")]
        self.assertEqual(arm["succ"], 1)
        self.assertEqual(arm["wall_times"], [2.0])
        self.assertEqual(arm["token_vals"], [])
        text = report.format_efficiency(arms, aggregated)
        self.assertIn("1/1", text)
        self.assertIn("2.00", text)
        self.assertIn("USAGE EVIDENCE", text)
        self.assertIn("proxy_mismatch", text)

    def test_candidate_proxy_only_fills_tok_slv_with_star(self):
        rows = [{
            "harness": "aider", "task": "t", "trial": 1, "success": True,
            "tokens": None, "turns": None, "wall_time_s": 10.0,
            "token_basis_proxy": "proxy_measured",
            "tokens_proxy_input_uncached": 40000,
            "tokens_proxy_output": 10000,
            "tokens_proxy_cache_read": 500000,
        }]
        arms, _tasks, stats = report.aggregate(rows)
        self.assertEqual(report.tokens_per_solve(stats[_arm("aider")]), 50000.0)
        text = report.format_efficiency(arms, stats)
        self.assertIn("50.0k*", text)
        self.assertIn("proxy-measured", text)
        self.assertIn("cache-read", text)

    def test_cache_read_does_not_inflate_fresh_total(self):
        rows = [{
            "harness": "aider", "task": "t", "trial": 1, "success": True,
            "tokens": None, "wall_time_s": 1.0,
            "token_basis_proxy": "proxy_measured",
            "tokens_proxy_input_uncached": 100,
            "tokens_proxy_output": 50,
            "tokens_proxy_cache_read": 999999,
        }]
        _, _, stats = report.aggregate(rows)
        self.assertEqual(report.tokens_per_solve(stats[_arm("aider")]), 150.0)

    def test_self_reported_preferred_over_proxy(self):
        rows = [{
            "harness": "pi", "task": "t", "trial": 1, "success": True,
            "tokens": 200, "wall_time_s": 1.0,
            "token_basis": "vendor_split",
            "token_basis_proxy": "proxy_measured",
            "tokens_proxy_input_uncached": 1000,
            "tokens_proxy_output": 1000,
            "tokens_proxy_cache_read": 5000,
        }]
        arms, _tasks, stats = report.aggregate(rows)
        self.assertEqual(report.tokens_per_solve(stats[_arm("pi")]), 200.0)
        text = report.format_efficiency(arms, stats)
        self.assertIn("200", text)
        self.assertNotIn("200*", text)
        self.assertNotIn(report.PROXY_FOOTNOTE, text)

    def test_mixed_basis_table_warns(self):
        rows = [
            {"harness": "pi", "model": "-", "task": "t", "trial": 1, "success": True,
             "tokens": 1000, "wall_time_s": 1.0, "token_basis": "vendor_split"},
            {"harness": "aider", "model": "-", "task": "t", "trial": 1, "success": True,
             "tokens": None, "wall_time_s": 1.0,
             "token_basis_proxy": "proxy_measured",
             "tokens_proxy_input_uncached": 400, "tokens_proxy_output": 100,
             "tokens_proxy_cache_read": 9000},
        ]
        arms, _tasks, stats = report.aggregate(rows)
        text = report.format_efficiency(arms, stats)
        self.assertIn("1.0k", text)   # pi self-reported, no star
        self.assertIn("500*", text)   # aider proxy
        self.assertIn(report.MIXED_BASIS_WARNING, text)

    def test_unmetered_stays_dash(self):
        rows = [{
            "harness": "quiet", "task": "t", "trial": 1, "success": True,
            "tokens": None, "wall_time_s": 1.0, "token_basis": "unmetered",
        }]
        arms, _tasks, stats = report.aggregate(rows)
        self.assertIsNone(report.tokens_per_solve(stats[_arm("quiet")]))
        text = report.format_efficiency(arms, stats)
        quiet = next(l for l in text.splitlines() if l.startswith("quiet"))
        self.assertNotIn("k", quiet)
        self.assertNotIn("*", quiet)

    def test_older_rows_without_proxy_unchanged(self):
        rows = rows_for("codex", [(True, 40000, None, 30.0), (True, 50000, None, 40.0)])
        arms, _tasks, stats = report.aggregate(rows)
        self.assertEqual(report.tokens_per_solve(stats[_arm("codex")]), 45000.0)
        text = report.format_efficiency(arms, stats)
        self.assertIn("45.0k", text)
        self.assertNotIn("*", text)
        self.assertNotIn(report.PROXY_FOOTNOTE, text)


class TestReportBuilders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "r.jsonl")
        rows = (rows_for("codexlike", [(True, 40000, None, 30.0), (True, 50000, None, 40.0)])
                + rows_for("quiet", [(True, None, None, 5.0), (True, None, None, 6.0)]))
        with open(self.path, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_report_unchanged_contains_wilson(self):
        # Regression guard: the success table still carries the Wilson header.
        self.assertIn("wilson95", report.build_report(self.path))

    def test_build_efficiency_report(self):
        text = report.build_efficiency_report(self.path)
        self.assertIn("turns/slv", text)
        # codexlike tokens/solve = 90000/2 = 45000 -> "45.0k"; quiet -> "-"
        self.assertIn("45.0k", text)
        quiet = next(l for l in text.splitlines() if l.startswith("quiet"))
        self.assertNotIn("k", quiet)

    def test_empty_results(self):
        empty = os.path.join(self.tmp, "empty.jsonl")
        open(empty, "w").close()
        self.assertIn("No results", report.build_efficiency_report(empty))


if __name__ == "__main__":
    unittest.main()
