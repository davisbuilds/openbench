#!/usr/bin/env python3
import contextlib
import io
import json
import os

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys
import tempfile
import unittest
from unittest import mock
from obench import candidate_gate
from obench import candidates


class CandidateGateTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.path.dirname(BENCH), "experiments", "candidates", "aider.toml")

    def runner(self, calls=1, timeout_class="timeout", failure_class="infra",
               failure_tokens=0, canary=False, calibration_solves=7):
        count = {"smoke": 0, "calibration": 0}
        def run(_harness, task, _model, _trial, timeout, *_args, **_kwargs):
            if task == "stall":
                return {"failure_class": timeout_class}
            count["smoke"] += 1
            if count["smoke"] == 1:
                return {"tokens_proxy_calls": calls, "harness_version": "aider 1.2.3",
                        "completed": True, "error": None,
                        "output_tail": "OPENBENCH_HOME_CANARY_7c51b9" if canary else "clean"}
            if count["smoke"] == 2:
                return {"failure_class": failure_class, "tokens": failure_tokens,
                        "tokens_proxy_calls": 0}
            solved = count["calibration"] < calibration_solves
            count["calibration"] += 1
            return {"success": solved}
        return run

    def live_gate(self, **kwargs):
        with mock.patch.object(candidates.ManifestHarness, "version", return_value="aider 1.2.3"):
            return candidate_gate.gate(
                self.path, "deepseek-v4-flash", live=True,
                cell_runner=self.runner(**kwargs),
                timeout_runner=lambda _seconds: {"failure_class": kwargs.get(
                    "timeout_class", "timeout")},
                proxy_ctx={"ledger_dir": "mock"})

    def test_dry_run_does_not_execute_version_or_cell(self):
        with mock.patch.object(candidates.ManifestHarness, "version", side_effect=AssertionError("live")):
            result = candidate_gate.gate(self.path, "deepseek-v4-flash")
        self.assertEqual(result["status"], "PASS")
        self.assertIn("--yes-always", result["preview"]["smoke"])
        self.assertTrue(all(item["status"] in {"PASS", "FAIL"} for item in result["checks"]))

    def test_live_mock_passes_all_checks_and_stamps_version(self):
        result = self.live_gate()
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(item["pass"] for item in result["checks"]))
        self.assertRegex(result["candidate_digest"], r"^[0-9a-f]{64}$")

    def test_metering_canary_timeout_and_honesty_failures_are_detected(self):
        result = self.live_gate(calls=0, canary=True, timeout_class="wrong_answer",
                                failure_class="wrong_answer", failure_tokens=500)
        checks = {item["name"]: item for item in result["checks"]}
        for name in ("METERING", "ISOLATION", "POLICY", "FAILURE HONESTY"):
            self.assertEqual(checks[name]["status"], "FAIL")

    def test_unmetered_manifest_allows_zero_proxy_calls(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "candidate.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="unmetered"\nunmetered=true\n'
                         'command=["cli", "--batch", "--yes"]\n'
                         'policy_headless_args=["--batch"]\n'
                         'policy_auto_approve_args=["--yes"]\n'
                         'version_command=["cli", "--version"]\n')
            with mock.patch.object(candidates.ManifestHarness, "version", return_value="aider 1.2.3"):
                result = candidate_gate.gate(path, "model", live=True,
                    cell_runner=self.runner(calls=0),
                    timeout_runner=lambda _seconds: {"failure_class": "timeout"},
                    proxy_ctx={"ledger_dir": "mock"})
        checks = {item["name"]: item for item in result["checks"]}
        self.assertEqual(checks["METERING"]["status"], "PASS")

    def test_calibration_reports_and_rejects_extremes(self):
        for solves in (0, 15):
            with mock.patch.object(candidates.ManifestHarness, "version", return_value="aider 1.2.3"):
                result = candidate_gate.gate(
                    self.path, "deepseek-v4-flash", live=True, calibrate=True,
                    cell_runner=self.runner(calibration_solves=solves),
                    timeout_runner=lambda _seconds: {"failure_class": "timeout"},
                    proxy_ctx={"ledger_dir": "mock"})
            check = next(item for item in result["checks"] if item["name"] == "CALIBRATION")
            self.assertEqual(check["status"], "FAIL")
            self.assertIn(f"solves={solves}/15", check["detail"])

    def test_policy_pins_are_required_and_unmetered_is_boolean(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "candidate.toml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\ncommand=["cli"]\nversion_command=["cli"]\n')
            result = candidate_gate.gate(path, "model")
            policy = next(item for item in result["checks"] if item["name"] == "POLICY")
            self.assertEqual(policy["status"], "FAIL")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('kind="manifest"\nname="bad"\nunmetered="yes"\ncommand=["cli"]\n')
            with self.assertRaisesRegex(ValueError, "unmetered must be a boolean"):
                candidates.load_candidate(path, os.path.join(BENCH, "adapters"))

    def test_output_has_check_lines_final_verdict_and_json(self):
        result = candidate_gate.gate(self.path, "deepseek-v4-flash")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            candidate_gate.print_result(result)
        text = out.getvalue()
        for name in ("METERING", "ISOLATION", "POLICY", "VERSION", "FAILURE HONESTY", "CALIBRATION"):
            self.assertRegex(text, rf"{name}: (PASS|FAIL)")
        self.assertIn("VERDICT: PASS", text)
        self.assertEqual(json.loads(text.split("JSON: ", 1)[1])["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
