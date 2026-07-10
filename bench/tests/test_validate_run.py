#!/usr/bin/env python3
"""Tests for the post-run validation gate."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

BENCH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BENCH_DIR)

import validate_run  # noqa: E402


def row(harness="h1", model="m1", task="t1", trial=1, **overrides):
    data = {
        "run_id": f"{harness}:{task}:{model}:trial{trial}",
        "harness": harness,
        "model": model,
        "task": task,
        "trial": trial,
        "success": False,
        "completed": True,
        "error": None,
        "wall_time_s": 10.0,
        "tokens": 100,
        "tokens_fresh": 100,
        "turns": 2,
        "checker_exit": 1,
        "failure_class": "wrong_answer",
    }
    data.update(overrides)
    return data


class ValidateRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bench_validate_run_")
        self.results = os.path.join(self.tmp, "fixture.jsonl")
        self.transcripts = os.path.join(self.tmp, "transcripts")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_rows(self, rows):
        with open(self.results, "w", encoding="utf-8") as fh:
            for item in rows:
                fh.write(json.dumps(item) + "\n")

    def rules(self, verdict):
        return {item["rule"] for item in verdict["findings"]}

    def test_passes_clean_fixture_without_expect(self):
        self.write_rows([
            row(trial=1),
            row(trial=2, success=True, completed=True, checker_exit=0, failure_class="solved"),
        ])
        verdict = validate_run.validate(self.results)
        self.assertTrue(verdict["pass"])
        self.assertEqual(verdict["findings"], [])

    def test_wrong_answer_missing_checker_stdout_is_info_only(self):
        self.write_rows([
            row(trial=1),
            row(trial=2, checker_stdout="checker details\n"),
            row(trial=3, checker_stdout="   \n"),
        ])
        verdict = validate_run.validate(self.results)
        self.assertTrue(verdict["pass"])
        self.assertEqual(verdict["findings"], [])
        infos = [f for f in verdict["infos"] if f["rule"] == "unauditable.missing_checker_stdout"]
        self.assertEqual(len(infos), 2)
        self.assertEqual({item["level"] for item in infos}, {"info"})
        self.assertEqual({item["suggested_action"] for item in infos}, {"none"})
        self.assertEqual([item["run_ids"] for item in infos], [["h1:t1:m1:trial1"], ["h1:t1:m1:trial3"]])

    def test_completeness_flags_duplicates_and_expected_holes(self):
        r1 = row(harness="h1", task="a", trial=1)
        r2 = row(harness="h1", task="a", trial=1)
        r2["run_id"] = "duplicate-run-id"
        self.write_rows([r1, r2])
        verdict = validate_run.validate(self.results, expect_arg="h1,h2")

        rules = self.rules(verdict)
        self.assertIn("completeness.duplicate", rules)
        self.assertIn("completeness.missing", rules)
        self.assertFalse(verdict["pass"])

    def test_completeness_flags_empty_results(self):
        self.write_rows([])
        verdict = validate_run.validate(self.results, expect_arg="h1")
        self.assertIn("completeness.empty", self.rules(verdict))
        self.assertEqual(verdict["findings"][0]["suggested_action"], "rerun")

    def test_invalid_json_reports_finding_without_crashing_completeness(self):
        with open(self.results, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row()) + "\n")
            fh.write("{not json\n")
        verdict = validate_run.validate(self.results)
        self.assertIn("json.invalid_row", self.rules(verdict))

    def test_taxonomy_sanity_rules(self):
        self.write_rows([
            row(task="rate", failure_class="rate_limited"),
            row(task="done-timeout", completed=True, failure_class="timeout"),
            row(task="success-wrong", success=True, checker_exit=0, failure_class="wrong_answer"),
            row(task="missing-class", failure_class=None),
            row(task="failed-solved", failure_class="solved"),
        ])
        verdict = validate_run.validate(self.results)
        self.assertIn("taxonomy.rate_limited", self.rules(verdict))
        self.assertIn("taxonomy.completed_timeout", self.rules(verdict))
        self.assertIn("taxonomy.success_not_solved", self.rules(verdict))
        self.assertIn("taxonomy.unknown_failure_class", self.rules(verdict))
        self.assertIn("taxonomy.failure_marked_solved", self.rules(verdict))
        self.assertEqual({f["suggested_action"] for f in verdict["findings"]}, {"rerun", "reclassify"})

    def test_contamination_sweep_flags_failure_transcript_with_three_markers(self):
        bad = row(run_id="h1:t1:m1:trial1", success=False, failure_class="wrong_answer")
        good = row(run_id="h1:t2:m1:trial1", task="t2", success=False, failure_class="wrong_answer")
        escaped = row(run_id="h1:t3:m1:trial1", task="t3", success=False,
                      failure_class="wrong_answer", transcript_path="../outside.txt")
        self.write_rows([bad, good, escaped])
        stem_dir = os.path.join(self.transcripts, "fixture")
        os.makedirs(stem_dir)
        with open(os.path.join(stem_dir, validate_run.sanitize_run_id(bad["run_id"]) + ".txt"), "w", encoding="utf-8") as fh:
            fh.write("HTTP 429\nprovider quota exhausted\nAPI rate limit exceeded\n")
        with open(os.path.join(stem_dir, validate_run.sanitize_run_id(good["run_id"]) + ".txt"), "w", encoding="utf-8") as fh:
            fh.write("HTTP 429 only once\n")
        with open(os.path.join(self.tmp, "outside.txt"), "w", encoding="utf-8") as fh:
            fh.write("HTTP 429\nquota\nrate limit exceeded\n")

        verdict = validate_run.validate(self.results, transcripts_dir=self.transcripts)
        findings = [f for f in verdict["findings"] if f["rule"] == "contamination.vendor_markers"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["run_ids"], [bad["run_id"]])
        self.assertEqual(findings[0]["suggested_action"], "investigate")

    def test_telemetry_rules(self):
        self.write_rows([
            row(run_id="solved-no-tokens", success=True, checker_exit=0, failure_class="solved", tokens=None, tokens_fresh=None),
            row(run_id="oom", error="exit 137", completed=False, wall_time_s=20.0, timeout_s=600, failure_class="infra"),
            row(run_id="checker-oom", checker_exit=137, completed=False, wall_time_s=20.0, timeout_s=600, failure_class="infra"),
            row(run_id="huge", turns=1, tokens=50001),
        ])
        verdict = validate_run.validate(self.results)
        rules = self.rules(verdict)
        self.assertIn("telemetry.solved_missing_tokens", rules)
        self.assertIn("telemetry.oom_exit_137", rules)
        self.assertIn("telemetry.single_turn_huge_tokens", rules)
        oom_findings = [f for f in verdict["findings"] if f["rule"] == "telemetry.oom_exit_137"]
        self.assertEqual(len(oom_findings), 2)

    def test_instant_fail_drift_guard(self):
        self.write_rows([
            row(
                run_id="codex:terminal-bench/cancel-async-tasks:gpt-5.6-sol:trial1",
                harness="codex",
                model="gpt-5.6-sol",
                task="terminal-bench/cancel-async-tasks",
                completed=False,
                error="exit 1",
                tokens=None,
                tokens_fresh=None,
                turns=None,
                wall_time_s=4.344,
                failure_class="wrong_answer",
            ),
        ])
        verdict = validate_run.validate(self.results)
        findings = [f for f in verdict["findings"] if f["rule"] == "instant_fails.classifier_drift"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["suggested_action"], "reclassify")

    def test_main_json_exit_codes(self):
        self.write_rows([row(success=True, checker_exit=0, failure_class="solved")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(validate_run.main(["--results", self.results, "--json"]), 0)
        self.write_rows([row(failure_class="rate_limited")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(validate_run.main(["--results", self.results, "--json"]), validate_run.EXIT_FINDINGS)


if __name__ == "__main__":
    unittest.main()
