import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from obench.codex_computer_use import (
    CodexComputerUseEvidenceError,
    summarize_events,
)


ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = ROOT / "computer-use-tasks/v0/scripts/official_codex_smoke.py"
SMOKE_SPEC = importlib.util.spec_from_file_location("official_codex_smoke", SMOKE_SCRIPT)
smoke = importlib.util.module_from_spec(SMOKE_SPEC)
assert SMOKE_SPEC.loader is not None
SMOKE_SPEC.loader.exec_module(smoke)

SCRIPTS = ROOT / "computer-use-tasks/v0/scripts"
sys.path.insert(0, str(SCRIPTS))
COMPARISON_SCRIPT = SCRIPTS / "official_vs_oss.py"
COMPARISON_SPEC = importlib.util.spec_from_file_location(
    "official_vs_oss", COMPARISON_SCRIPT
)
comparison = importlib.util.module_from_spec(COMPARISON_SPEC)
assert COMPARISON_SPEC.loader is not None
COMPARISON_SPEC.loader.exec_module(comparison)


class CodexComputerUseTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.events = Path(self.temporary.name) / "events.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, *events):
        self.events.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

    def test_summarizes_proven_computer_use_calls(self):
        result = {
            "content": [{"type": "text", "text": "updated"}],
            "_meta": {
                "codex/toolSurface": {"kind": "computerUse"},
                "codex/nodeReplExecutionDurationMs": 12.5,
            },
        }
        self._write(
            {"type": "thread.started", "thread_id": "thread-1"},
            {
                "type": "item.completed",
                "item": {
                    "id": "call-1",
                    "type": "mcp_tool_call",
                    "server": "node_repl",
                    "tool": "js",
                    "arguments": {
                        "code": "await sky.click({app: 'Fixture', target: 'toggle'})"
                    },
                    "result": result,
                    "status": "completed",
                },
            },
        )

        summary = summarize_events(self.events)

        self.assertEqual(summary["call_count"], 1)
        self.assertEqual(summary["failed_call_count"], 0)
        self.assertEqual(summary["total_execution_ms"], 12.5)
        self.assertEqual(summary["total_model_visible_text_bytes"], 7)
        self.assertEqual(summary["calls"][0]["tool"], "click")
        self.assertEqual(summary["calls"][0]["status"], "completed")
        self.assertEqual(summary["calls"][0]["semantic_tools"], ["click"])

    def test_rejects_calls_without_computer_use_surface_evidence(self):
        self._write({
            "type": "item.completed",
            "item": {
                "id": "call-1",
                "type": "mcp_tool_call",
                "server": "node_repl",
                "tool": "js",
                "arguments": {"code": "await sky.list_apps()"},
                "result": {
                    "content": [{"type": "text", "text": "apps"}],
                    "_meta": {"codex/nodeReplExecutionDurationMs": 1},
                },
                "status": "completed",
            },
        })

        with self.assertRaisesRegex(
            CodexComputerUseEvidenceError, "lacks successful surface evidence"
        ):
            summarize_events(self.events)

    def test_fixture_timeout_is_normalized_for_fail_closed_cleanup(self):
        with mock.patch.object(
            smoke.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["cub_v0.py", "reset"], 60),
        ):
            with self.assertRaisesRegex(smoke.SmokeError, "fixture reset failed"):
                smoke._run_cub(Path("request.toml"), "reset", 1)

    def test_fixture_verify_exit_one_is_a_terminal_wrong_answer(self):
        completed = subprocess.CompletedProcess(
            ["cub_v0.py", "verify"], 1, stdout="", stderr="wrong answer"
        )
        with mock.patch.object(smoke.subprocess, "run", return_value=completed):
            self.assertEqual(smoke._run_cub(Path("request.toml"), "verify", 1), 1)

    def test_fixture_verify_exit_two_is_infrastructure_invalid(self):
        completed = subprocess.CompletedProcess(
            ["cub_v0.py", "verify"], 2, stdout="", stderr="broken verifier"
        )
        with mock.patch.object(smoke.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(smoke.SmokeError, "fixture verify failed"):
                smoke._run_cub(Path("request.toml"), "verify", 1)

    def test_tree_digest_is_content_and_path_sensitive(self):
        root = Path(self.temporary.name) / "module"
        root.mkdir()
        (root / "index.js").write_text("one\n", encoding="utf-8")
        original = smoke._tree_sha256(root)

        (root / "index.js").write_text("two\n", encoding="utf-8")
        changed = smoke._tree_sha256(root)
        self.assertNotEqual(changed, original)

        (root / "index.js").rename(root / "main.js")
        self.assertNotEqual(smoke._tree_sha256(root), changed)

    def test_official_evidence_bundle_is_sealed_and_tamper_evident(self):
        root = Path(self.temporary.name) / "evidence"
        (root / "agent").mkdir(parents=True)
        (root / "result.json").write_text("{}\n", encoding="utf-8")
        (root / "agent/events.jsonl").write_text("{}\n", encoding="utf-8")

        digest = smoke.seal_evidence_bundle(root)
        self.assertEqual(smoke.verify_evidence_bundle(root), digest)

        (root / "agent/events.jsonl").write_text('{"changed":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(smoke.SmokeError, "does not match"):
            smoke.verify_evidence_bundle(root)

    def test_service_runtime_identity_binds_socket_owner_to_executable(self):
        executable = Path(self.temporary.name) / "service"
        executable.write_bytes(b"service")
        socket_path = Path(self.temporary.name) / "service.sock"
        owner = subprocess.CompletedProcess(
            ["lsof"], 0, stdout="123\n", stderr=""
        )
        process = subprocess.CompletedProcess(
            ["ps"], 0, stdout=f"{executable.resolve()}\n", stderr=""
        )
        with mock.patch.object(smoke.subprocess, "run", side_effect=[owner, process]):
            identity = smoke._service_runtime_identity(socket_path, executable)

        self.assertEqual(identity["pid"], 123)
        self.assertEqual(identity["executable_path"], str(executable.resolve()))
        self.assertEqual(identity["executable_sha256"], smoke._sha256(executable))

    def test_service_runtime_identity_rejects_different_executable(self):
        executable = Path(self.temporary.name) / "service"
        executable.write_bytes(b"service")
        owner = subprocess.CompletedProcess(
            ["lsof"], 0, stdout="123\n", stderr=""
        )
        process = subprocess.CompletedProcess(
            ["ps"], 0, stdout="/other/service\n", stderr=""
        )
        with mock.patch.object(smoke.subprocess, "run", side_effect=[owner, process]):
            with self.assertRaisesRegex(smoke.SmokeError, "not running the expected"):
                smoke._service_runtime_identity(
                    Path(self.temporary.name) / "service.sock", executable
                )

    def test_service_runtime_identity_accepts_exit_between_probes(self):
        executable = Path(self.temporary.name) / "service"
        executable.write_bytes(b"service")
        owner = subprocess.CompletedProcess(
            ["lsof"], 0, stdout="123\n", stderr=""
        )
        exited = subprocess.CompletedProcess(
            ["ps"], 0, stdout="", stderr=""
        )
        with mock.patch.object(smoke.subprocess, "run", side_effect=[owner, exited]):
            self.assertIsNone(smoke._service_runtime_identity(
                Path(self.temporary.name) / "service.sock",
                executable,
                allow_missing=True,
            ))


class ComputerUseComparisonContractTests(unittest.TestCase):
    def test_official_wrong_answer_requires_completed_agent_and_verdict(self):
        comparison._require_official_terminal(
            {"agent_completed": True, "verifier_exit": 1, "passed": False}, 1, 1
        )
        with self.assertRaisesRegex(comparison.ComparisonError, "terminal verifier"):
            comparison._require_official_terminal(
                {"agent_completed": False, "verifier_exit": None}, 1, 1
            )

    def test_official_return_code_must_match_verdict(self):
        with self.assertRaisesRegex(comparison.ComparisonError, "infrastructure-invalid"):
            comparison._require_official_terminal(
                {"agent_completed": True, "verifier_exit": 0, "passed": True}, 1, 1
            )

    def test_official_pass_flag_must_agree_with_verdict(self):
        with self.assertRaisesRegex(comparison.ComparisonError, "inconsistent"):
            comparison._require_official_terminal(
                {"agent_completed": True, "verifier_exit": 1, "passed": True}, 1, 1
            )

    def test_oss_wrong_answer_requires_completed_row_and_checker_verdict(self):
        comparison._require_oss_terminal(
            {"completed": True, "checker_exit": 1, "score": 0.0}, 0, 1
        )
        with self.assertRaisesRegex(comparison.ComparisonError, "terminal checker"):
            comparison._require_oss_terminal(
                {"completed": False, "checker_exit": None}, 0, 1
            )

    def test_oss_nonstandard_checker_exit_is_infrastructure_invalid(self):
        with self.assertRaisesRegex(comparison.ComparisonError, "terminal checker"):
            comparison._require_oss_terminal(
                {"completed": True, "checker_exit": 17, "score": 0.0}, 0, 1
            )

    def test_oss_nonzero_runner_exit_is_infrastructure_invalid(self):
        with self.assertRaisesRegex(comparison.ComparisonError, "infrastructure-invalid"):
            comparison._require_oss_terminal(
                {"completed": True, "checker_exit": 0, "score": 1.0}, 2, 1
            )

    def test_oss_checker_score_must_agree_with_verdict(self):
        with self.assertRaisesRegex(comparison.ComparisonError, "inconsistent"):
            comparison._require_oss_terminal(
                {"completed": True, "checker_exit": 1, "score": 1.0}, 0, 1
            )


if __name__ == "__main__":
    unittest.main()
