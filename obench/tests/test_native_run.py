from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
import unittest

from obench.native_macos import (
    FocusEvent,
    LeaseUnavailableError,
    PreflightCheck,
    PreflightResult,
    WholeRunLease,
)
from obench.native_run import NativeRunError, NativeRunHooks, load_config, run_native
from obench.native_trial import NativeTrialError, load_native_trial


HEX_A = "a" * 64


class FakeFocusMonitor:
    def __init__(self, bundle_id="com.openbench.fixture"):
        self.started = False
        self.stopped = False
        self.violations = ()
        self.bundle_id = bundle_id
        self.events = ()

    def start(self):
        self.started = True
        self.events = (FocusEvent(
            self.bundle_id,
            "Fixture",
            123,
            0.0,
            datetime.now(timezone.utc).isoformat(),
        ),)

    def stop(self):
        self.stopped = True


class FakeAdapter:
    def __init__(self, *, fail=False, retry_once=False):
        self.fail = fail
        self.retry_once = retry_once
        self.calls = 0

    def run(self, instruction, workdir, model, timeout_s):
        self.calls += 1
        if self.retry_once and self.calls == 1:
            return {
                "completed": False,
                "error": "transient startup",
                "startup_failure": True,
            }
        workspace = Path(workdir)
        request = {
            "jsonrpc": "2.0",
            "id": self.calls,
            "method": "tools/call",
            "params": {"name": "click", "arguments": {"target": "fixture"}},
        }
        launcher = os.environ["CUB_MCP_COMMAND"]
        completed = subprocess.run(
            [launcher],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0:
            return {"completed": False, "error": completed.stderr}
        trajectory = {
            "schema_version": "ATIF-v1.7",
            "agent": {"name": "replaced", "version": "replaced"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "Complete the fixture task."},
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "Fixture task completed.",
                    "metrics": {
                        "prompt_tokens": 10,
                        "cached_tokens": 2,
                        "completion_tokens": 3,
                    },
                },
            ],
            "final_metrics": {
                "total_steps": 2,
                "total_prompt_tokens": 10,
                "total_cached_tokens": 2,
                "total_completion_tokens": 3,
            },
        }
        (workspace / "trajectory.json").write_text(json.dumps(trajectory), encoding="utf-8")
        (workspace / "state.json").write_text(
            json.dumps({"saved": True}), encoding="utf-8"
        )
        return {
            "completed": not self.fail,
            "error": "agent failed" if self.fail else None,
        }


class NativeRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native_run_test_")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "instruction.md").write_text(
            "Complete the fixture task.\n", encoding="utf-8"
        )
        self.phase_script = self.root / "phase.py"
        self.phase_script.write_text(
            textwrap.dedent(
                """
                import json
                from pathlib import Path
                import sys

                phase = sys.argv[1]
                workspace = Path.cwd()
                if (workspace / ("fail-" + phase)).exists():
                    raise SystemExit(17)
                if phase == "verifier":
                    (workspace / "verdict.json").write_text(
                        json.dumps({"score": 1.0, "checker_exit": 0})
                    )
                if phase == "reset":
                    path = workspace / "reset.log"
                    path.write_text(path.read_text() + "reset\\n" if path.exists() else "reset\\n")
                """
            ),
            encoding="utf-8",
        )
        self.mcp_script = self.root / "mcp.py"
        self.mcp_script.write_text(
            textwrap.dedent(
                """
                import json
                import sys

                for line in sys.stdin:
                    request = json.loads(line)
                    response = {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "content": [],
                            "isError": False,
                            "_meta": {
                                "computer-use-mcp/delivery": {
                                    "delivery_tier": "tier1-ax-attribute",
                                    "fallback_reasons": [],
                                }
                            },
                        },
                    }
                    print(json.dumps(response), flush=True)
                """
            ),
            encoding="utf-8",
        )
        self.config_path = self.root / "native.toml"
        self._write_config()

    def tearDown(self):
        self.temp.cleanup()

    def _write_config(self):
        command = json.dumps([sys.executable, str(self.mcp_script)])
        phase = json.dumps([sys.executable, str(self.phase_script), "PHASE"])
        content = f'''\
schema_version = "openbench.native-run.v0"
trial_id = "native-fixture-trial1"
output_dir = "bundle"
results_path = "results.jsonl"
lease_path = "{self.root / 'lease.lock'}"
workspace = "workspace"
atif_path = "trajectory.json"
verdict_path = "verdict.json"

[task]
id = "computer-use-fixture"
instruction = "workspace/instruction.md"

[harness]
name = "fixture-harness"
version = "1.2.3"
version_source = "fixture"

[model]
name = "fixture-model"
provider = "fixture-provider"
revision = "fixture-model-2026-08-01"

[mcp]
name = "computer-use-mcp"
version = "0.9.0"
command = {command}
client_command_env = "CUB_MCP_COMMAND"

[environment]
architecture = "arm64"
hardware_model = "MacFixture1,1"
mcp_bundle_id = "com.example.computer-use"

[environment.os]
version = "15.6"
build = "24G84"

[environment.app]
bundle_id = "com.openbench.fixture"
version = "1.2.3"
build = "45"
code_signature_sha256 = "{HEX_A}"

[environment.display]
width_px = 1728
height_px = 1117
scale_factor = 2.0
color_space = "Display P3"

[budget]
timeout_s = 30
max_retries = 1

[focus]
required_foreground_bundle_id = "com.openbench.fixture"
forbidden_bundle_ids = []
require_foreground_full_agent_phase = true
forbid_global_delivery = true
allowed_delivery_tiers = ["tier1-ax-attribute"]

[phases.setup]
command = {phase.replace('PHASE', 'setup')}
timeout_s = 5

[phases.verifier]
command = {phase.replace('PHASE', 'verifier')}
timeout_s = 5

[phases.reset]
command = {phase.replace('PHASE', 'reset')}
timeout_s = 5

[[artifacts]]
source = "state.json"
path = "artifacts/final-state/state.json"
media_type = "application/json"
'''
        self.config_path.write_text(content, encoding="utf-8")

    @staticmethod
    def _preflight(passed=True):
        return PreflightResult((
            PreflightCheck(
                "screen_unlocked",
                passed,
                passed,
                True,
                "fixture session evidence",
            ),
        ))

    def _hooks(self, adapter=None, *, preflight=True):
        monitor = FakeFocusMonitor()
        hooks = NativeRunHooks(
            preflight=lambda spec: self._preflight(preflight),
            focus_monitor_factory=lambda allowed: monitor,
            adapter_loader=lambda config: adapter or FakeAdapter(),
            version_probe=lambda config, loaded: "1.2.3",
        )
        return hooks, monitor

    def test_success_generates_imports_and_seals_exact_native_bundle(self):
        hooks, monitor = self._hooks()
        outcome = run_native(self.config_path, hooks=hooks)

        self.assertTrue(monitor.started)
        self.assertTrue(monitor.stopped)
        self.assertTrue(outcome.bundle_dir.is_dir())
        self.assertEqual(outcome.row["exec_mode"], "native_macos")
        self.assertEqual(outcome.row["success"], True)
        self.assertNotIn("harbor", json.dumps(outcome.row).lower())
        self.assertEqual(len(outcome.results_path.read_text().splitlines()), 1)
        self.assertEqual(load_native_trial(outcome.bundle_dir)["run_id"], outcome.row["run_id"])

    def test_whole_run_lease_conflict_prevents_runtime_work(self):
        config = load_config(self.config_path)
        adapter_calls = []
        hooks, _ = self._hooks()
        hooks.adapter_loader = lambda value: adapter_calls.append(value)
        with WholeRunLease(config.lease_path):
            with self.assertRaises(LeaseUnavailableError):
                run_native(config, hooks=hooks)
        self.assertEqual(adapter_calls, [])

    def test_locked_session_fails_before_phases_or_adapter(self):
        adapter = FakeAdapter()
        hooks, monitor = self._hooks(adapter, preflight=False)
        with self.assertRaisesRegex(Exception, "preflight failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertEqual(adapter.calls, 0)
        self.assertFalse(monitor.started)

    def test_setup_failure_still_resets_and_stops_monitor(self):
        (self.workspace / "fail-setup").touch()
        hooks, monitor = self._hooks()
        with self.assertRaisesRegex(NativeRunError, "setup phase failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue((self.workspace / "reset.log").is_file())
        self.assertTrue(monitor.stopped)

    def test_agent_failure_still_resets_and_stops_monitor(self):
        hooks, monitor = self._hooks(FakeAdapter(fail=True))
        with self.assertRaisesRegex(NativeRunError, "agent failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue((self.workspace / "reset.log").is_file())
        self.assertTrue(monitor.stopped)

    def test_verifier_failure_still_resets_and_stops_monitor(self):
        (self.workspace / "fail-verifier").touch()
        hooks, monitor = self._hooks()
        with self.assertRaisesRegex(NativeRunError, "verifier phase failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue((self.workspace / "reset.log").is_file())
        self.assertTrue(monitor.stopped)

    def test_reset_failure_fails_closed_after_successful_agent_and_verifier(self):
        (self.workspace / "fail-reset").touch()
        hooks, monitor = self._hooks()
        with self.assertRaisesRegex(NativeRunError, "reset phase failed"):
            run_native(self.config_path, hooks=hooks)
        self.assertTrue(monitor.stopped)
        self.assertFalse((self.root / "bundle").exists())

    def test_transient_pre_token_startup_retry_retains_every_attempt(self):
        adapter = FakeAdapter(retry_once=True)
        hooks, _ = self._hooks(adapter)
        outcome = run_native(self.config_path, hooks=hooks)
        records = sorted(outcome.attempts_dir.glob("attempt*/attempt.json"))
        self.assertEqual(len(records), 2)
        first = json.loads(records[0].read_text())
        self.assertTrue(first["retry"])
        self.assertEqual(outcome.row["candidate_provenance"]["retry_count"], 1)

    def test_tampered_generated_bundle_is_rejected(self):
        hooks, _ = self._hooks()
        outcome = run_native(self.config_path, hooks=hooks)
        artifact = outcome.bundle_dir / "artifacts/final-state/state.json"
        artifact.write_text('{"saved":false}', encoding="utf-8")
        with self.assertRaises(NativeTrialError):
            load_native_trial(outcome.bundle_dir)

    def test_cli_help_and_missing_argument_smoke(self):
        repo = Path(__file__).parents[2]
        help_result = subprocess.run(
            [sys.executable, "-m", "obench", "native", "--help"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("native macOS Computer-Use", help_result.stdout)
        missing = subprocess.run(
            [sys.executable, "-m", "obench", "native", "run"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("config", missing.stderr)


if __name__ == "__main__":
    unittest.main()
