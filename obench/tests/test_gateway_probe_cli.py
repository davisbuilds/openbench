import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from obench import gateway_cli, gateway_probe_cli, gateway_probe_run
from obench.gateway_probe_models import RunSummary
from obench.tests.test_gateway_probe_report import row
from obench.tests.test_gateway_probe_spec import manifest


class GatewayProbeCliTests(unittest.TestCase):
    def test_checked_in_minimal_and_four_way_examples_validate_through_cli(self):
        examples = Path(__file__).parents[1] / "examples"
        cases = (
            ("gateway-probe-responses.toml", "arms=2"),
            ("gateway-probe-four-way-responses.toml", "arms=4"),
        )
        for filename, expected in cases:
            with self.subTest(filename=filename):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = gateway_probe_cli.main([
                        "validate",
                        str(examples / filename),
                    ])
                self.assertEqual(code, 0)
                self.assertIn(expected, stdout.getvalue())

    def test_gateway_dispatches_nested_probe_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp, "probe.toml")
            spec.write_text(manifest(), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gateway_cli.main(["probe", "validate", str(spec)])
        self.assertEqual(code, 0)
        self.assertIn("valid probe=probe-test", stdout.getvalue())

    def test_doctor_is_offline_and_fails_closed_for_missing_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp, "probe.toml")
            spec.write_text(manifest(), encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = gateway_probe_cli.main(["doctor", str(spec)])
        self.assertEqual(code, 2)
        self.assertIn('"live_requests": false', stdout.getvalue())
        self.assertIn("OPENAI_API_KEY", stdout.getvalue())

    def test_run_dispatches_without_live_request_when_runner_is_mocked(self):
        summary = RunSummary(Path("out.jsonl"), 4, 2, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp, "probe.toml")
            spec.write_text(manifest(), encoding="utf-8")
            with mock.patch.object(
                gateway_probe_run, "run_experiment", return_value=summary
            ) as run:
                code = gateway_probe_cli.main(["run", str(spec), "--results", "out.jsonl"])
        self.assertEqual(code, 0)
        run.assert_called_once()

    def test_malformed_prices_and_missing_credentials_exit_two_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp, "probe.toml")
            spec.write_text(manifest(), encoding="utf-8")
            cases = [
                (
                    ["doctor", str(spec)],
                    {gateway_probe_run.FROZEN_PRICES_ENV: "{bad-json"},
                    "not valid JSON",
                ),
                (
                    ["run", str(spec), "--results", str(Path(tmp, "out.jsonl"))],
                    {
                        gateway_probe_run.FROZEN_PRICES_ENV: json.dumps({
                            "openai/gpt-4o-mini": {
                                "input_per_million": "1",
                                "output_per_million": "2",
                                "effective_at": "2026-07-25T00:00:00Z",
                            }
                        })
                    },
                    "missing or empty",
                ),
            ]
            for argv, environ, expected in cases:
                with self.subTest(command=argv[0]):
                    stderr = io.StringIO()
                    with mock.patch.dict("os.environ", environ, clear=True):
                        with contextlib.redirect_stderr(stderr):
                            code = gateway_probe_cli.main(argv)
                    self.assertEqual(code, 2)
                    self.assertIn(expected, stderr.getvalue())
                    self.assertNotIn("Traceback", stderr.getvalue())

    def test_report_cli_renders_publishable_tables(self):
        rows = [
            row("direct", "cold", 1, baseline=True),
            row("gateway", "cold", 1, total=1.5),
            row("direct", "warm", 1, baseline=True),
            row("gateway", "warm", 1, total=1.5),
        ]
        for item in rows:
            item["scheduled_blocks_per_condition"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp, "probe.jsonl")
            results.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = gateway_probe_cli.main(["report", str(results)])
        self.assertEqual(code, 0)
        self.assertIn("Gateway Probe (exploratory)", stdout.getvalue())
        self.assertIn("| Arm | Condition |", stdout.getvalue())
        self.assertIn("| Gateway | Condition | Median delta TTFT |", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
