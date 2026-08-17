import json
from pathlib import Path
import tempfile
import unittest

from obench.codex_computer_use import (
    CodexComputerUseEvidenceError,
    summarize_events,
)


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
        self.assertEqual(summary["total_execution_ms"], 12.5)
        self.assertEqual(summary["total_model_visible_text_bytes"], 7)
        self.assertEqual(summary["calls"][0]["tool"], "click")
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


if __name__ == "__main__":
    unittest.main()
