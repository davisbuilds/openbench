from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from obench.native_matrix import canonical_bytes
from obench.native_run import main
from obench.tests.test_native_report import (
    HARNESS,
    MCP_A,
    MCP_B,
    MODEL,
    TASK,
    _plan,
    _row,
)


def _invoke(argv):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code)
    return code, stdout.getvalue(), stderr.getvalue()


def _write_json(path, value):
    path.write_bytes(canonical_bytes(value) + b"\n")


class NativeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native_cli_test_")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def plan_spec(self, repetitions=2):
        return {
            "comparison_id": "cub-v0",
            "task": TASK,
            "harness": HARNESS,
            "model": MODEL,
            "arms": [
                {"id": "baseline", "mcp": MCP_A},
                {"id": "candidate", "mcp": MCP_B},
            ],
            "repetitions": repetitions,
        }

    def compile_plan(self, repetitions=2):
        spec = self.root / "spec.json"
        output = self.root / "plan.json"
        _write_json(spec, self.plan_spec(repetitions))
        code, stdout, stderr = _invoke(
            ["plan", str(spec), "--output", str(output)]
        )
        self.assertEqual(code, 0, stderr)
        return output, json.loads(stdout)

    def test_plan_write_is_canonical_idempotent_and_refuses_divergence(self):
        spec = self.root / "spec.json"
        output = self.root / "plan.json"
        _write_json(spec, self.plan_spec())

        code, stdout, stderr = _invoke(
            ["plan", str(spec), "--output", str(output)]
        )
        self.assertEqual(code, 0, stderr)
        first = output.read_bytes()
        self.assertEqual(first, canonical_bytes(json.loads(first)) + b"\n")
        self.assertEqual(json.loads(stdout)["write_status"], "created")

        code, stdout, stderr = _invoke(
            ["plan", str(spec), "--output", str(output)]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["write_status"], "unchanged")
        self.assertEqual(output.read_bytes(), first)

        _write_json(spec, self.plan_spec(repetitions=3))
        code, _stdout, stderr = _invoke(
            ["plan", str(spec), "--output", str(output)]
        )
        self.assertEqual(code, 2)
        self.assertIn("refusing to overwrite divergent native plan", stderr)
        self.assertEqual(output.read_bytes(), first)

    def test_plan_output_rejects_symlink(self):
        spec = self.root / "spec.json"
        target = self.root / "target.json"
        output = self.root / "plan.json"
        _write_json(spec, self.plan_spec())
        target.write_text("unchanged\n", encoding="utf-8")
        output.symlink_to(target)

        code, _stdout, stderr = _invoke(
            ["plan", str(spec), "--output", str(output)]
        )
        self.assertEqual(code, 2)
        self.assertIn("must not be a symlink", stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged\n")

    def test_state_reconciles_prior_evidence_without_in_place_replacement(self):
        plan_path, _summary = self.compile_plan(repetitions=1)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        state_one = self.root / "state-one.json"
        code, stdout, stderr = _invoke(
            ["state", str(plan_path), "--output", str(state_one)]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["pending_cells"], 2)

        cell = plan["schedule"][0]
        observation = {
            key: cell[key]
            for key in ("cell_id", "trial_id", "config_sha256", "cell_sha256")
        }
        observation.update(
            {"result_sha256": "a" * 64, "bundle_sha256": "b" * 64}
        )
        observation_path = self.root / "observation.json"
        _write_json(observation_path, observation)
        state_two = self.root / "state-two.json"
        code, stdout, stderr = _invoke(
            [
                "state",
                str(plan_path),
                "--prior-state",
                str(state_one),
                "--observation",
                str(observation_path),
                "--output",
                str(state_two),
            ]
        )
        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["completed_cells"], 1)
        self.assertEqual(summary["pending_cells"], 1)
        self.assertEqual(
            json.loads(state_one.read_text(encoding="utf-8"))["completed"], []
        )

        conflicting = {**observation, "result_sha256": "c" * 64}
        _write_json(observation_path, conflicting)
        state_three = self.root / "state-three.json"
        code, _stdout, stderr = _invoke(
            [
                "state",
                str(plan_path),
                "--prior-state",
                str(state_two),
                "--observation",
                str(observation_path),
                "--output",
                str(state_three),
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("different immutable result evidence", stderr)
        self.assertFalse(state_three.exists())

    def test_report_exit_codes_complete_incomplete_and_noncomparable(self):
        plan = _plan(repetitions=1)
        plan_path = self.root / "report-plan.json"
        _write_json(plan_path, plan)
        complete_rows = self.root / "complete.jsonl"
        complete_rows.write_text(
            "\n".join(
                json.dumps(_row(plan, arm, 1), sort_keys=True)
                for arm in ("baseline", "candidate")
            )
            + "\n",
            encoding="utf-8",
        )
        complete_report = self.root / "complete-report.json"
        code, stdout, stderr = _invoke(
            [
                "report",
                str(plan_path),
                "--results",
                str(complete_rows),
                "--output",
                str(complete_report),
            ]
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            json.loads(stdout)["publication_status"],
            "complete_row_bound_bundle_not_revalidated",
        )

        incomplete_rows = self.root / "incomplete.jsonl"
        incomplete_rows.write_text(
            json.dumps(_row(plan, "baseline", 1), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        incomplete_report = self.root / "incomplete-report.json"
        code, stdout, stderr = _invoke(
            [
                "report",
                str(plan_path),
                "--results",
                str(incomplete_rows),
                "--output",
                str(incomplete_report),
            ]
        )
        self.assertEqual(code, 3, stderr)
        self.assertTrue(incomplete_report.is_file())
        self.assertEqual(
            json.loads(stdout)["publication_status"],
            "incomplete_noncomparable_cells_excluded",
        )

        noncomparable = _row(plan, "baseline", 1)
        noncomparable["model"] = "different-model"
        bad_rows = self.root / "noncomparable.jsonl"
        bad_rows.write_text(
            json.dumps(noncomparable, sort_keys=True) + "\n", encoding="utf-8"
        )
        bad_report = self.root / "bad-report.json"
        code, _stdout, stderr = _invoke(
            [
                "report",
                str(plan_path),
                "--results",
                str(bad_rows),
                "--output",
                str(bad_report),
            ]
        )
        self.assertEqual(code, 4)
        self.assertIn("NONCOMPARABLE", stderr)
        self.assertFalse(bad_report.exists())

    def test_help_smoke_is_unambiguous_and_has_no_plan_execution_command(self):
        repo = Path(__file__).parents[2]
        result = subprocess.run(
            [sys.executable, "-m", "obench", "native", "--help"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("{run,plan,state,report}", result.stdout)
        self.assertNotIn("execute", result.stdout)
        for command in ("run", "plan", "state", "report"):
            child = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "obench",
                    "native",
                    command,
                    "--help",
                ],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(child.returncode, 0, child.stderr)


if __name__ == "__main__":
    unittest.main()
