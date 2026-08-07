#!/usr/bin/env python3
"""Protocol fixture tests for the transparent MCP stdio collector."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from obench import mcp_stdio_collector as collector


SECRET = "private-value-that-must-not-appear"


ECHO_FIXTURE = r"""
import json
import sys

for raw in sys.stdin.buffer:
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        continue
    if message.get("method") == "tools/call":
        name = message["params"]["name"]
        duplicate_response = (
            message["params"].get("arguments", {}).get("duplicate_response") is True
        )
        if name == "crash":
            sys.exit(23)
        if name == "rpc_error":
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32001, "message": "fixture failure"},
            }
        else:
            if name == "sensitive_meta":
                meta = {
                    "computer-use-mcp/error": {
                        "code": "private-value-that-must-not-appear",
                        "recovery": "private-value-that-must-not-appear",
                    },
                    "computer-use-mcp/outcome": {
                        "classification": "private-value-that-must-not-appear",
                        "failure_domain": "private-value-that-must-not-appear",
                        "summary": "private-value-that-must-not-appear",
                        "verification": {
                            "before_value_preview": "private-value-that-must-not-appear",
                            "notes": ["private-value-that-must-not-appear"],
                            "rendered_text_changed": True,
                        },
                    },
                    "computer-use-mcp/focus": {
                        "frontmost_before": {
                            "name": "private-value-that-must-not-appear"
                        },
                        "focus_changed": False,
                    },
                    "computer-use-mcp/delivery": {
                        "delivery_tier": "private-value-that-must-not-appear",
                        "fallback_reasons": [
                            "private-value-that-must-not-appear"
                        ],
                        "chain_rung": "private-value-that-must-not-appear",
                        "ui_changed": True,
                    },
                }
            else:
                meta = {
                    "computer-use-mcp/error": (
                        {"code": "ELEMENT_NOT_FOUND"} if name == "tool_error" else None
                    ),
                    "computer-use-mcp/outcome": {
                        "classification": "success",
                        "failure_domain": None,
                    },
                    "computer-use-mcp/focus": {"focus_changed": False},
                    "computer-use-mcp/delivery": {
                        "delivery_tier": "tier1-ax-action"
                    },
                    "unrelated": {"must": "not be collected"},
                }
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [{"type": "text", "text": "ok"}],
                    "isError": name == "tool_error",
                    "_meta": meta,
                },
            }
        sys.stdout.buffer.write(
            json.dumps(response, separators=(",", ":")).encode() + b"\n"
        )
        if duplicate_response:
            sys.stdout.buffer.write(
                json.dumps(response, separators=(",", ":")).encode() + b"\n"
            )
    else:
        sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()
"""


def rpc(method, request_id, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


class BlockingAfterFirstRead:
    def __init__(self, first):
        self.first = first
        self.release = threading.Event()
        self.finished = threading.Event()

    def read(self, _size):
        if self.first is not None:
            first, self.first = self.first, None
            return first
        self.release.wait(5)
        self.finished.set()
        return b""


class DelayedFirstRead:
    def __init__(self, payload):
        self.payload = payload
        self.release = threading.Event()
        self.finished = threading.Event()

    def read(self, _size):
        if self.payload is None:
            return b""
        self.release.wait(5)
        payload, self.payload = self.payload, None
        self.finished.set()
        return payload


class FailingOutput:
    def write(self, _data):
        raise OSError("fixture downstream closed")

    def flush(self):
        pass


class MCPStdioCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mcp_collector_")
        self.fixture = Path(self.tmp.name) / "fixture.py"
        self.fixture.write_text(textwrap.dedent(ECHO_FIXTURE), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_fixture(self, payload, *, name="ledger.jsonl", fixture=None):
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        result = collector.collect_stdio(
            [sys.executable, str(fixture or self.fixture)],
            ledger_path=Path(self.tmp.name) / name,
            run_id="run-1",
            trial_id=name,
            stdin=io.BytesIO(payload),
            stdout=stdout,
            stderr=stderr,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        rows = [
            json.loads(line)
            for line in result.ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        return result, stdout.getvalue(), stderr.getvalue(), rows

    def test_passthrough_preserves_initialize_list_and_unrelated_bytes(self):
        payload = (
            rpc("initialize", 1, {"clientInfo": {"name": "fixture"}})
            + rpc("tools/list", "list-1")
            + b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        result, output, stderr, rows = self.run_fixture(payload)
        self.assertEqual(output, payload)
        self.assertEqual(stderr, b"")
        self.assertEqual(result.call_count, 0)
        self.assertTrue(result.integrity_ok)
        self.assertEqual([row["record_type"] for row in rows], ["ledger_seal"])

    def test_correlates_call_and_collects_structured_meta_and_byte_counts(self):
        request = rpc(
            "tools/call",
            "call-1",
            {"name": "click", "arguments": {"id": "e12@s3", "confirm": True}},
        )
        result, output, _, rows = self.run_fixture(request)
        response = output
        call = rows[0]
        self.assertEqual(result.call_count, 1)
        self.assertTrue(result.integrity_ok)
        self.assertEqual(call["tool"], "click")
        self.assertEqual(call["status"], "completed")
        self.assertEqual(call["request_bytes"], len(request))
        self.assertEqual(call["response_bytes"], len(response))
        self.assertGreaterEqual(call["duration_ms"], 0)
        self.assertEqual(
            call["computer_use_meta"]["delivery"]["delivery_tier"],
            "tier1-ax-action",
        )
        self.assertEqual(
            call["computer_use_meta"]["outcome"]["classification"], "success"
        )
        self.assertNotIn("unrelated", call["computer_use_meta"])
        verified = collector.verify_ledger(result.ledger_path)
        self.assertEqual(verified.root_hash, result.root_hash)

    def test_tool_and_jsonrpc_errors_are_distinct(self):
        payload = (
            rpc("tools/call", 1, {"name": "tool_error", "arguments": {}})
            + rpc("tools/call", 2, {"name": "rpc_error", "arguments": {}})
        )
        result, _, _, rows = self.run_fixture(payload)
        self.assertTrue(result.integrity_ok)
        self.assertEqual([row["status"] for row in rows[:-1]], [
            "tool_error",
            "jsonrpc_error",
        ])
        self.assertTrue(rows[0]["tool_is_error"])
        self.assertEqual(
            rows[0]["computer_use_meta"]["error"]["code"], "ELEMENT_NOT_FOUND"
        )
        self.assertEqual(rows[1]["jsonrpc_error"], {"present": True, "code": -32001})

    def test_duplicate_tool_response_is_forwarded_but_non_clean(self):
        request = rpc(
            "tools/call",
            14,
            {
                "name": "click",
                "arguments": {"duplicate_response": True},
            },
        )
        result, output, _, rows = self.run_fixture(
            request, name="duplicate-response.jsonl"
        )
        response_lines = output.splitlines()
        self.assertEqual(len(response_lines), 2)
        self.assertEqual(response_lines[0], response_lines[1])
        self.assertEqual(result.call_count, 1)
        self.assertEqual(result.malformed_frames, 1)
        self.assertFalse(result.integrity_ok)
        self.assertEqual(rows[0]["status"], "completed")

    def test_cross_method_request_id_collision_cannot_complete_tool_call(self):
        fixture = Path(self.tmp.name) / "cross-method-collision.py"
        fixture.write_text(
            "import json\n"
            "import sys\n"
            "first = json.loads(sys.stdin.buffer.readline())\n"
            "second = json.loads(sys.stdin.buffer.readline())\n"
            "response = {'jsonrpc': '2.0', 'id': second['id'], 'result': {'tools': []}}\n"
            "sys.stdout.buffer.write(json.dumps(response).encode() + b'\\n')\n"
            "sys.stdout.buffer.flush()\n",
            encoding="utf-8",
        )
        payload = (
            rpc("tools/call", 15, {"name": "click", "arguments": {}})
            + rpc("tools/list", 15)
        )
        result, output, _, rows = self.run_fixture(
            payload, name="cross-method.jsonl", fixture=fixture
        )
        self.assertIn(b'"tools": []', output)
        self.assertEqual(result.call_count, 1)
        self.assertEqual(rows[0]["status"], "missing_response")
        self.assertEqual(rows[-1]["summary"]["duplicate_request_ids"], 1)
        self.assertFalse(result.integrity_ok)

    def test_argument_digest_is_stable_but_contains_no_raw_values(self):
        arguments = {
            "text": SECRET,
            "password": "hunter2",
            "point": [123.5, 999],
            "confirm": True,
        }
        request = rpc(
            "tools/call", "secret-call", {"name": "type_text", "arguments": arguments}
        )
        result, _, _, rows = self.run_fixture(request)
        ledger_bytes = result.ledger_path.read_bytes()
        self.assertNotIn(SECRET.encode(), ledger_bytes)
        self.assertNotIn(b"hunter2", ledger_bytes)
        self.assertNotIn(b"123.5", ledger_bytes)
        self.assertEqual(
            rows[0]["argument_digest"],
            collector.normalized_argument_digest(arguments),
        )
        changed_values = dict(arguments, text="another-private-value")
        self.assertEqual(
            collector.normalized_argument_digest(arguments),
            collector.normalized_argument_digest(changed_values),
        )

    def test_unrecognized_tool_name_is_not_persisted(self):
        request = rpc(
            "tools/call", 11, {"name": SECRET, "arguments": {}}
        )
        result, _, _, rows = self.run_fixture(request, name="safe-tool.jsonl")
        self.assertNotIn(SECRET.encode(), result.ledger_path.read_bytes())
        self.assertEqual(rows[0]["tool"], "<unrecognized>")

    def test_oversized_argument_shape_is_correlated_but_non_clean(self):
        arguments = {"items": [0] * collector.MAX_ARGUMENT_NODES}
        with self.assertRaises(collector.ArgumentDigestLimitError):
            collector.normalized_argument_digest(arguments)
        request = rpc(
            "tools/call", 13, {"name": "click", "arguments": arguments}
        )
        result, _, _, rows = self.run_fixture(
            request, name="argument-budget.jsonl"
        )
        self.assertEqual(result.call_count, 1)
        self.assertFalse(result.integrity_ok)
        self.assertEqual(result.malformed_frames, 1)
        self.assertEqual(
            rows[0]["argument_digest"], "<unavailable:complexity-limit>"
        )

    def test_sensitive_meta_is_reduced_to_safe_categories_and_booleans(self):
        request = rpc(
            "tools/call", 6, {"name": "sensitive_meta", "arguments": {}}
        )
        result, _, _, rows = self.run_fixture(request, name="safe-meta.jsonl")
        self.assertNotIn(SECRET.encode(), result.ledger_path.read_bytes())
        meta = rows[0]["computer_use_meta"]
        self.assertEqual(meta["error"]["code"], "<unrecognized>")
        self.assertEqual(meta["outcome"]["classification"], "<unrecognized>")
        self.assertEqual(
            meta["outcome"]["verification"], {"rendered_text_changed": True}
        )
        self.assertEqual(meta["focus"], {"focus_changed": False})
        self.assertEqual(meta["delivery"]["delivery_tier"], "<unrecognized>")
        self.assertEqual(meta["delivery"]["fallback_reasons"], ["<unrecognized>"])
        self.assertTrue(meta["delivery"]["ui_changed"])

    def test_malformed_and_partial_messages_pass_through_and_fail_integrity(self):
        fixture = Path(self.tmp.name) / "malformed.py"
        fixture.write_text(
            "import sys\n"
            "data = sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(data + b'{\\\"partial\\\":')\n"
            "sys.stdout.buffer.flush()\n",
            encoding="utf-8",
        )
        payload = b"{bad json}\n" + rpc("tools/list", 5) + b'{"partial":'
        result, output, _, rows = self.run_fixture(
            payload, name="malformed.jsonl", fixture=fixture
        )
        self.assertEqual(output, payload + b'{"partial":')
        self.assertFalse(result.integrity_ok)
        self.assertEqual(result.malformed_frames, 2)
        self.assertEqual(result.partial_frames, 2)
        self.assertEqual(rows[-1]["summary"]["malformed_frames"], 2)

    def test_child_crash_records_missing_response_and_seals_non_cleanly(self):
        request = rpc(
            "tools/call", 7, {"name": "crash", "arguments": {"text": SECRET}}
        )
        blocked_input = BlockingAfterFirstRead(request)
        stdout = io.BytesIO()
        ledger_path = Path(self.tmp.name) / "crash.jsonl"
        result = collector.collect_stdio(
            [sys.executable, str(self.fixture)],
            ledger_path=ledger_path,
            run_id="run-1",
            trial_id="crash",
            stdin=blocked_input,
            stdout=stdout,
            stderr=io.BytesIO(),
        )
        sealed_bytes = ledger_path.read_bytes()
        blocked_input.release.set()
        self.assertTrue(blocked_input.finished.wait(1))
        self.assertEqual(ledger_path.read_bytes(), sealed_bytes)
        rows = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(result.returncode, 23)
        self.assertEqual(stdout.getvalue(), b"")
        self.assertFalse(result.integrity_ok)
        self.assertEqual(result.missing_responses, 1)
        self.assertEqual(rows[0]["status"], "missing_response")
        self.assertEqual(rows[0]["process_returncode"], 23)
        collector.verify_ledger(result.ledger_path)

    def test_clean_child_exit_marks_inflight_stdin_read_incomplete(self):
        fixture = Path(self.tmp.name) / "clean-exit.py"
        fixture.write_text("", encoding="utf-8")
        delayed_input = DelayedFirstRead(
            rpc("tools/call", 12, {"name": "click", "arguments": {}})
        )
        ledger_path = Path(self.tmp.name) / "inflight-input.jsonl"
        result = collector.collect_stdio(
            [sys.executable, str(fixture)],
            ledger_path=ledger_path,
            run_id="run-1",
            trial_id="inflight-input",
            stdin=delayed_input,
            stdout=io.BytesIO(),
            stderr=io.BytesIO(),
        )
        sealed_bytes = ledger_path.read_bytes()
        delayed_input.release.set()
        self.assertTrue(delayed_input.finished.wait(1))
        self.assertEqual(ledger_path.read_bytes(), sealed_bytes)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.input_incomplete)
        self.assertFalse(result.integrity_ok)
        verified = collector.verify_ledger(ledger_path)
        self.assertTrue(verified.summary["input_incomplete"])

    def test_downstream_failure_kills_chatty_child_and_leaves_unsealed_ledger(self):
        fixture = Path(self.tmp.name) / "chatty.py"
        fixture.write_text(
            "import sys\n"
            "sys.stdin.buffer.readline()\n"
            "while True:\n"
            "    sys.stdout.buffer.write(b'x' * 65536)\n"
            "    sys.stdout.buffer.flush()\n",
            encoding="utf-8",
        )
        ledger_path = Path(self.tmp.name) / "relay-failure.jsonl"
        started = time.monotonic()
        with self.assertRaises(collector.CollectorError):
            collector.collect_stdio(
                [sys.executable, str(fixture)],
                ledger_path=ledger_path,
                run_id="run-1",
                trial_id="relay-failure",
                stdin=io.BytesIO(rpc("tools/list", 1)),
                stdout=FailingOutput(),
                stderr=io.BytesIO(),
                max_frame_bytes=32,
            )
        self.assertLess(time.monotonic() - started, 2)
        with self.assertRaises(collector.LedgerIntegrityError):
            collector.verify_ledger(ledger_path)

    @unittest.skipUnless(os.name == "posix", "process-group fixture requires POSIX")
    def test_wrapper_descendant_cannot_hold_relay_pipe_open(self):
        fixture = Path(self.tmp.name) / "wrapper.py"
        fixture.write_text(
            "import os\n"
            "import sys\n"
            "import time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    sys.stdout.buffer.write(b'partial-from-descendant')\n"
            "    sys.stdout.buffer.flush()\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "time.sleep(0.1)\n"
            "os._exit(0)\n",
            encoding="utf-8",
        )
        started = time.monotonic()
        result, output, _, _ = self.run_fixture(
            b"", name="descendant.jsonl", fixture=fixture
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(output, b"partial-from-descendant")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.integrity_ok)
        self.assertEqual(result.partial_frames, 1)
        collector.verify_ledger(result.ledger_path)

    def test_oversized_unterminated_frame_uses_bounded_parser_memory(self):
        ledger = collector.CallLedger(
            Path(self.tmp.name) / "bounded.jsonl", "run-1", "bounded"
        )
        observer = collector._ProtocolObserver(ledger, max_frame_bytes=128)
        try:
            observer.feed("request", b"x" * (1024 * 1024))
            self.assertEqual(len(observer._buffers["request"]), 0)
            self.assertTrue(observer._discarding["request"])
            self.assertEqual(observer.malformed_frames, 1)
            observer.feed(
                "request",
                b"discarded-tail\n"
                + rpc("tools/call", 10, {"name": "click", "arguments": {}}),
            )
            self.assertFalse(observer._discarding["request"])
            self.assertEqual(len(observer._pending), 1)
        finally:
            ledger.abort()

    def test_partial_ledger_and_tampering_fail_verification(self):
        request = rpc("tools/call", 9, {"name": "click", "arguments": {}})
        result, _, _, _ = self.run_fixture(request, name="integrity.jsonl")
        original = result.ledger_path.read_bytes()

        partial = Path(self.tmp.name) / "partial-ledger.jsonl"
        partial.write_bytes(original[:-1])
        with self.assertRaises(collector.LedgerIntegrityError):
            collector.verify_ledger(partial)

        records = [json.loads(line) for line in original.splitlines()]
        records[0]["tool"] = "tampered"
        tampered = Path(self.tmp.name) / "tampered-ledger.jsonl"
        tampered.write_bytes(
            b"\n".join(
                json.dumps(row, separators=(",", ":")).encode() for row in records
            )
            + b"\n"
        )
        with self.assertRaises(collector.LedgerIntegrityError):
            collector.verify_ledger(tampered)

        records = [json.loads(line) for line in original.splitlines()]
        records[-1]["summary"]["integrity_ok"] = False
        tampered_seal = Path(self.tmp.name) / "tampered-seal.jsonl"
        tampered_seal.write_bytes(
            b"\n".join(
                json.dumps(row, separators=(",", ":")).encode() for row in records
            )
            + b"\n"
        )
        with self.assertRaises(collector.LedgerIntegrityError):
            collector.verify_ledger(tampered_seal)

    def test_invalid_summary_is_rejected_before_terminal_seal(self):
        path = Path(self.tmp.name) / "invalid-summary.jsonl"
        ledger = collector.CallLedger(path, "run-1", "invalid-summary")
        try:
            with self.assertRaises(ValueError):
                ledger.seal(
                    {
                        "returncode": 9,
                        "integrity_ok": True,
                        "malformed_frames": 0,
                        "partial_frames": 0,
                        "duplicate_request_ids": 0,
                        "missing_responses": 0,
                    }
                )
            self.assertEqual(path.read_bytes(), b"")
            with self.assertRaises(collector.LedgerIntegrityError):
                collector.verify_ledger(path)
        finally:
            ledger.abort()

    def test_existing_ledger_is_never_overwritten(self):
        path = Path(self.tmp.name) / "existing.jsonl"
        path.write_bytes(b"owned\n")
        with self.assertRaises(FileExistsError):
            collector.collect_stdio(
                [sys.executable, str(self.fixture)],
                ledger_path=path,
                run_id="run-1",
                trial_id="trial-1",
                stdin=io.BytesIO(b""),
                stdout=io.BytesIO(),
                stderr=io.BytesIO(),
            )
        self.assertEqual(path.read_bytes(), b"owned\n")


if __name__ == "__main__":
    unittest.main()
