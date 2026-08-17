import importlib.util
import json
from pathlib import Path
import subprocess
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


if __name__ == "__main__":
    unittest.main()
