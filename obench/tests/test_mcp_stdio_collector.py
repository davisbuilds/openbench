#!/usr/bin/env python3
"""Protocol fixture tests for the transparent MCP stdio collector."""

from __future__ import annotations

import io
import json
import os
import select
import signal
import subprocess
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
            arguments = message["params"].get("arguments", {})
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
            if "metrics" in arguments:
                meta["computer-use-mcp/metrics"] = arguments["metrics"]
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

    def run_fixture(
        self, payload, *, name="ledger.jsonl", fixture=None, call_contract=(),
        state_response_mode=None,
    ):
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
            call_contract=call_contract,
            state_response_mode=state_response_mode,
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

    def test_owner_marker_binds_child_to_collector_and_command(self):
        owner_path = Path(self.tmp.name) / "owner.json"
        command = [sys.executable, str(self.fixture)]
        collector.collect_stdio(
            command,
            ledger_path=Path(self.tmp.name) / "owner-ledger.jsonl",
            run_id="run-owner",
            trial_id="trial-owner",
            owner_path=owner_path,
            stdin=io.BytesIO(rpc("initialize", 1)),
            stdout=io.BytesIO(),
            stderr=io.BytesIO(),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        marker = json.loads(owner_path.read_text(encoding="utf-8"))
        self.assertEqual(
            marker["schema_version"],
            "openbench.mcp-process-owner.v1",
        )
        self.assertEqual(marker["state"], "ready")
        self.assertEqual(marker["collector_pid"], os.getpid())
        self.assertGreater(marker["child_pid"], 0)
        self.assertEqual(
            marker["command_sha256"],
            collector._command_sha256(command),
        )
        self.assertEqual(owner_path.stat().st_mode & 0o777, 0o600)

    def test_sigterm_from_mcp_client_gracefully_seals_ledger(self):
        ledger_path = Path(self.tmp.name) / "signal-ledger.jsonl"
        owner_path = Path(self.tmp.name) / "signal-owner.json"
        program = f"""
import sys
from obench.mcp_stdio_collector import collect_stdio
result = collect_stdio(
    [sys.executable, {str(self.fixture)!r}],
    ledger_path={str(ledger_path)!r},
    owner_path={str(owner_path)!r},
    run_id="run-signal",
    trial_id="trial-signal",
    stdin=sys.stdin.buffer,
    stdout=sys.stdout.buffer,
    stderr=sys.stderr.buffer,
)
raise SystemExit(result.returncode)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(program)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parents[2],
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not owner_path.exists():
            time.sleep(0.01)
        self.assertTrue(owner_path.exists())

        process.terminate()
        _stdout, stderr = process.communicate(timeout=5)

        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        verified = collector.verify_ledger(ledger_path)
        self.assertTrue(verified.integrity_ok)
        self.assertEqual(verified.summary["returncode"], 0)
        self.assertEqual(verified.summary["relay_failures"], 0)

    @unittest.skipUnless(os.name == "posix", "process-group fixture requires POSIX")
    def test_sigterm_forces_hung_child_group_and_seals_non_cleanly(self):
        ledger_path = Path(self.tmp.name) / "hung-signal-ledger.jsonl"
        owner_path = Path(self.tmp.name) / "hung-signal-owner.json"
        child_pid_path = Path(self.tmp.name) / "hung-child.pid"
        hung_fixture = Path(self.tmp.name) / "hung-after-eof.py"
        hung_fixture.write_text(
            "import os\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            f"Path({str(child_pid_path)!r}).write_text(str(os.getpid()))\n"
            "sys.stdin.buffer.read()\n"
            "while True:\n"
            "    time.sleep(1)\n",
            encoding="utf-8",
        )
        program = f"""
import sys
from obench.mcp_stdio_collector import collect_stdio
result = collect_stdio(
    [sys.executable, {str(hung_fixture)!r}],
    ledger_path={str(ledger_path)!r},
    owner_path={str(owner_path)!r},
    run_id="run-hung-signal",
    trial_id="trial-hung-signal",
    stdin=sys.stdin.buffer,
    stdout=sys.stdout.buffer,
    stderr=sys.stderr.buffer,
)
raise SystemExit(result.returncode)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(program)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=Path(__file__).parents[2],
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if owner_path.exists() and child_pid_path.exists():
                break
            time.sleep(0.01)
        self.assertTrue(owner_path.exists())
        self.assertTrue(child_pid_path.exists())

        process.terminate()
        _stdout, stderr = process.communicate(timeout=5)

        self.assertNotEqual(process.returncode, 0, stderr.decode(errors="replace"))
        verified = collector.verify_ledger(ledger_path)
        self.assertFalse(verified.integrity_ok)
        self.assertEqual(verified.summary["returncode"], -signal.SIGKILL)
        self.assertEqual(verified.summary["relay_failures"], 1)
        rows = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(rows[-1]["record_type"], "ledger_seal")

        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        self.assertEqual(
            json.loads(owner_path.read_text(encoding="utf-8"))["child_pid"],
            child_pid,
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_buffered_live_input_is_forwarded_before_eof(self):
        input_read_fd, input_write_fd = os.pipe()
        output_read_fd, output_write_fd = os.pipe()
        input_reader = os.fdopen(input_read_fd, "rb")
        input_writer = os.fdopen(input_write_fd, "wb", buffering=0)
        output_reader = os.fdopen(output_read_fd, "rb", buffering=0)
        output_writer = os.fdopen(output_write_fd, "wb", buffering=0)
        errors = []

        def run_collector():
            try:
                collector.collect_stdio(
                    [sys.executable, str(self.fixture)],
                    ledger_path=Path(self.tmp.name) / "live-ledger.jsonl",
                    run_id="run-live",
                    trial_id="trial-live",
                    stdin=input_reader,
                    stdout=output_writer,
                    stderr=io.BytesIO(),
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_collector)
        thread.start()
        try:
            request = rpc("initialize", 1, {"clientInfo": {"name": "fixture"}})
            input_writer.write(request)
            ready, _, _ = select.select([output_reader], [], [], 3)
            self.assertEqual(ready, [output_reader])
            self.assertEqual(os.read(output_reader.fileno(), len(request)), request)
        finally:
            input_writer.close()
            thread.join(timeout=5)
            input_reader.close()
            output_reader.close()
            output_writer.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

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
        self.assertIsNone(call["computer_use_meta"]["metrics"])
        verified = collector.verify_ledger(result.ledger_path)
        self.assertEqual(verified.root_hash, result.root_hash)

    def test_retains_operation_metrics_in_hash_chained_ledger(self):
        metrics = {
            "schema_version": 1,
            "operation": {
                "operation": "click",
                "tool": "click",
                "app_bundle_identifier": "com.apple.TextEdit",
                "ax_role": "AXButton",
                "attempted_delivery_strategies": ["ax-action", "cg-event"],
                "final_delivery_strategy": "ax-action",
                "effect_outcome": "verified",
                "queue_latency_ms": 3,
                "execution_latency_ms": 17,
            },
        }
        request = rpc(
            "tools/call", 20, {"name": "click", "arguments": {"metrics": metrics}}
        )
        result, output, _, rows = self.run_fixture(
            request, name="operation-metrics.jsonl"
        )

        self.assertIn(b'"computer-use-mcp/metrics"', output)
        self.assertTrue(result.integrity_ok)
        self.assertEqual(rows[0]["computer_use_meta"]["metrics"], metrics)
        verified = collector.verify_ledger(result.ledger_path)
        self.assertEqual(verified.call_count, 1)
        self.assertEqual(verified.root_hash, result.root_hash)

    def test_retains_perception_only_and_combined_metrics(self):
        perception = {
            "operation": "get_app_state",
            "tool": "get_app_state",
            "app_bundle_identifier": None,
            "elapsed_ms": 41,
            "elements_visited": 120,
            "elements_returned": 35,
            "partial": False,
            "diff": True,
            "context_bytes": 4096,
        }
        operation = {
            "operation": "click",
            "tool": "click",
            "attempted_delivery_strategies": [],
            "queue_latency_ms": 0,
            "execution_latency_ms": 8,
        }
        envelopes = (
            {"schema_version": 1, "perception": perception},
            {
                "schema_version": 1,
                "operation": operation,
                "perception": perception,
            },
        )
        for index, metrics in enumerate(envelopes):
            with self.subTest(metrics=metrics):
                request = rpc(
                    "tools/call",
                    21 + index,
                    {"name": "get_app_state", "arguments": {"metrics": metrics}},
                )
                result, _, _, rows = self.run_fixture(
                    request, name=f"perception-metrics-{index}.jsonl"
                )
                self.assertTrue(result.integrity_ok)
                self.assertEqual(rows[0]["computer_use_meta"]["metrics"], metrics)
                self.assertTrue(collector.verify_ledger(result.ledger_path).integrity_ok)

    def test_retains_new_perception_sizes_encoding_and_phase_timings(self):
        perception = {
            "operation": "set_value",
            "tool": "set_value",
            "perception_ms": 41,
            "settle_ms": 3,
            "screenshot_ms": 0,
            "snapshot_ms": 25,
            "verification_ms": 2,
            "response_construction_ms": 8,
            "other_ms": 3,
            "elements_visited": 120,
            "elements_returned": 35,
            "partial": False,
            "response_encoding": "full",
            "text_bytes": 2048,
            "screenshot_png_bytes": 0,
        }
        metrics = {"schema_version": 2, "perception": perception}
        request = rpc(
            "tools/call", 29, {"name": "set_value", "arguments": {"metrics": metrics}}
        )
        result, _, _, rows = self.run_fixture(request, name="new-perception.jsonl")
        self.assertTrue(result.integrity_ok)
        self.assertEqual(rows[0]["computer_use_meta"]["metrics"], metrics)

    def test_accepts_scoped_outcome_response_encoding(self):
        perception = {
            "operation": "click",
            "tool": "click",
            "perception_ms": 41,
            "settle_ms": 3,
            "screenshot_ms": 0,
            "snapshot_ms": 25,
            "verification_ms": 2,
            "response_construction_ms": 8,
            "other_ms": 3,
            "elements_visited": 120,
            "elements_returned": 0,
            "partial": False,
            "response_encoding": "outcome",
            "text_bytes": 384,
            "screenshot_png_bytes": 0,
        }
        metrics = {"schema_version": 2, "perception": perception}
        request = rpc(
            "tools/call", 30, {"name": "click", "arguments": {"metrics": metrics}}
        )
        result, _, _, rows = self.run_fixture(
            request, name="outcome-perception.jsonl"
        )
        self.assertTrue(result.integrity_ok)
        self.assertEqual(rows[0]["computer_use_meta"]["metrics"], metrics)

    def test_call_contract_records_only_required_argument_projection(self):
        contract = [{
            "tool": "click",
            "required_arguments": {
                "include_state": True,
                "include_screenshot": False,
            },
        }]
        request = rpc("tools/call", 28, {
            "name": "click",
            "arguments": {
                "target": "e12@s3",
                "include_state": True,
                "include_screenshot": False,
            },
        })
        _, _, _, rows = self.run_fixture(
            request, name="contract.jsonl", call_contract=contract
        )
        self.assertEqual(rows[0]["contract_sequence"], 1)
        self.assertEqual(
            rows[0]["contract_arguments"], contract[0]["required_arguments"]
        )
        self.assertNotIn("target", json.dumps(rows[0]))

    def test_locked_state_mode_is_injected_before_contract_observation(self):
        contract = [{
            "tool": "click",
            "required_arguments": {
                "include_state": True,
                "include_screenshot": False,
                "state_response_mode": "full",
            },
        }]
        request = rpc("tools/call", 27, {
            "name": "click",
            "arguments": {
                "target": "e12@s3",
                "include_state": True,
                "include_screenshot": False,
            },
        })
        result, _, _, rows = self.run_fixture(
            request,
            name="mode-injection.jsonl",
            call_contract=contract,
            state_response_mode="full",
        )
        self.assertTrue(result.integrity_ok)
        self.assertEqual(rows[0]["contract_arguments"], contract[0]["required_arguments"])
        conflicting = request.replace(
            b'"target":"e12@s3"',
            b'"state_response_mode":"auto","target":"e12@s3"',
        )
        overridden, _, _, rows = self.run_fixture(
            conflicting,
            name="mode-conflict.jsonl",
            call_contract=contract,
            state_response_mode="full",
        )
        self.assertTrue(overridden.integrity_ok)
        self.assertEqual(rows[0]["contract_arguments"], contract[0]["required_arguments"])

    def test_rejects_malformed_known_metrics_metadata(self):
        valid_operation = {
            "operation": "click",
            "tool": "click",
            "attempted_delivery_strategies": [],
            "queue_latency_ms": 0,
            "execution_latency_ms": 8,
        }
        malformed = (
            {"schema_version": 1.0, "operation": valid_operation},
            {"schema_version": 1},
            {
                "schema_version": 1,
                "operation": {**valid_operation, "queue_latency_ms": True},
            },
            {
                "schema_version": 1,
                "operation": {**valid_operation, "unexpected": "field"},
            },
            {
                "schema_version": 1,
                "operation": {**valid_operation, "operation": "\ud800"},
            },
            {
                "schema_version": 1,
                "perception": {
                    "operation": "get_app_state",
                    "tool": "get_app_state",
                    "elapsed_ms": 1,
                    "elements_visited": 2,
                    "elements_returned": 2,
                    "partial": 0,
                    "diff": False,
                    "context_bytes": 128,
                },
            },
            {
                "schema_version": 2,
                "perception": {
                    "operation": "get_app_state",
                    "tool": "get_app_state",
                    "perception_ms": 1,
                    "settle_ms": 1,
                    "screenshot_ms": 0,
                    "snapshot_ms": 0,
                    "verification_ms": 0,
                    "response_construction_ms": 0,
                    "other_ms": 0,
                    "elements_visited": 2,
                    "elements_returned": 2,
                    "partial": False,
                    "response_encoding": "auto",
                },
            },
            {
                "schema_version": 2,
                "perception": {
                    "operation": "get_app_state",
                    "tool": "get_app_state",
                    "perception_ms": 1,
                    "settle_ms": 0,
                    "screenshot_ms": 0,
                    "snapshot_ms": 0,
                    "verification_ms": 0,
                    "response_construction_ms": 0,
                    "other_ms": 0,
                    "elements_visited": 2,
                    "elements_returned": 2,
                    "partial": False,
                    "response_encoding": "auto",
                    "text_bytes": 10,
                    "screenshot_png_bytes": 0,
                },
            },
        )
        for index, metrics in enumerate(malformed):
            with self.subTest(metrics=metrics):
                request = rpc(
                    "tools/call",
                    30 + index,
                    {"name": "click", "arguments": {"metrics": metrics}},
                )
                result, output, _, rows = self.run_fixture(
                    request, name=f"malformed-metrics-{index}.jsonl"
                )
                self.assertIn(b'"computer-use-mcp/metrics"', output)
                self.assertFalse(result.integrity_ok)
                self.assertEqual(result.malformed_frames, 1)
                self.assertIsNone(rows[0]["computer_use_meta"]["metrics"])
                self.assertEqual(
                    rows[0]["computer_use_meta"]["delivery"]["delivery_tier"],
                    "tier1-ax-action",
                )
                self.assertFalse(
                    collector.verify_ledger(result.ledger_path).integrity_ok
                )

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

    def test_downstream_failure_kills_child_and_seals_non_cleanly(self):
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
        result = collector.collect_stdio(
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
        self.assertFalse(result.integrity_ok)
        self.assertGreaterEqual(result.relay_failures, 1)
        verified = collector.verify_ledger(ledger_path)
        self.assertFalse(verified.integrity_ok)
        self.assertEqual(
            verified.summary["relay_failures"],
            result.relay_failures,
        )

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
