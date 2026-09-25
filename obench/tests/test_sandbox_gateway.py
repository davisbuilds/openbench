"""Real-socket contracts for the isolated inference broker and fixed relay."""

from __future__ import annotations
import http.client
import http.server
import json
from pathlib import Path
import socket
import select
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock

from obench import sandbox_gateway as gateway

BODY = {
    "model": "gpt-5.6-terra",
    "reasoning": {"effort": "xhigh"},
    "stream": True,
    "store": False,
    "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "repair source"}]}
    ],
}
SECRET = "private-oauth-token-never-log"


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket = Path(self.tmp.name) / "gateway.sock"
        self.requests = []
        self.receipts = []
        self.block_stream = False
        self.upstream_body = None
        self.upstream_waiting = threading.Event()
        self.upstream_closed = threading.Event()
        self.upstream_status = 200
        self.upstream_content_type = "text/event-stream"
        self.upstream_prefix = b''
        self.stall_headers = False
        owner = self

        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((self.path, dict(self.headers), json.loads(body)))
                if owner.stall_headers:
                    owner.upstream_waiting.set()
                    self.connection.settimeout(5)
                    try:
                        if self.connection.recv(1) == b"":
                            owner.upstream_closed.set()
                    except OSError:
                        owner.upstream_closed.set()
                    return
                if owner.upstream_status != 200:
                    self.send_response(owner.upstream_status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(SECRET.encode())
                    return
                self.send_response(200)
                if owner.upstream_content_type is not None:
                    self.send_header("Content-Type", owner.upstream_content_type)
                self.end_headers()
                self.wfile.write(owner.upstream_prefix)
                # Cancellation tests must stall before the terminal event.
                body = owner.upstream_body
                if body is None:
                    body = (b'data: {"type":"response.created"}\n\n' if owner.block_stream else
                            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":11,"output_tokens":7}}}\n\ndata: [DONE]\n\n')
                self.wfile.write(body)
                self.wfile.flush()
                if owner.block_stream:
                    owner.upstream_waiting.set()
                    self.connection.settimeout(5)
                    try:
                        if self.connection.recv(1) == b"":
                            owner.upstream_closed.set()
                    except OSError:
                        owner.upstream_closed.set()

        self.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.up_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.up_thread.start()

        def transport(body, headers, timeout):
            conn = http.client.HTTPConnection(
                *self.upstream.server_address, timeout=timeout
            )
            conn.request("POST", "/fixed-upstream", body, headers)
            return gateway.UpstreamStream(conn, conn.getresponse())

        self.config = gateway.GatewayConfig(
            model="gpt-5.6-terra",
            effort="xhigh",
            max_requests=5,
            max_body_bytes=65536,
            timeout_seconds=3,
        )
        self.broker = gateway.BrokerServer(
            self.socket,
            self.config,
            gateway.AuthCredentials(SECRET, "account-private"),
            transport=transport,
            receipt=self.receipts.append,
        )
        self.thread = threading.Thread(target=self.broker.serve_forever, daemon=True)
        self.thread.start()
        self.relay = None

    def tearDown(self):
        if self.relay:
            self.relay.stop()
        self.broker.stop()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join(3)
        self.up_thread.join(3)
        self.tmp.cleanup()

    def request(self, body=None, path="/responses", headers=None, relay=False):
        conn = (
            http.client.HTTPConnection(*self.relay.server_address, timeout=3)
            if relay
            else UnixConnection(self.socket)
        )
        payload = json.dumps(BODY if body is None else body)
        try:
            try:
                conn.request(
                    "POST",
                    path,
                    payload,
                    {"Content-Type": "application/json", **(headers or {})},
                )
            except BrokenPipeError:
                pass  # An early framing refusal can race the client's body send.
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_real_unix_round_trip_injects_auth_and_logs_only_metadata(self):
        status, raw = self.request(headers={"Authorization": "Bearer fake-agent-key"})
        self.assertEqual(status, 200)
        self.assertIn(b"[DONE]", raw)
        path, headers, body = self.requests[0]
        self.assertEqual(headers["Authorization"], "Bearer " + SECRET)
        self.assertEqual(headers["ChatGPT-Account-Id"], "account-private")
        self.assertEqual(body, BODY)
        self.assertEqual(
            self.receipts[0]["upstream_peer"],
            {
                "ip": self.upstream.server_address[0],
                "port": self.upstream.server_address[1],
            },
        )
        self.assertEqual(
            self.receipts[0]["usage"], {"input_tokens": 11, "output_tokens": 7}
        )
        self.assertNotIn(SECRET, json.dumps(self.receipts))
        self.assertNotIn("repair source", json.dumps(self.receipts))
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o660)

    def test_real_loopback_relay_to_fixed_unix_socket(self):
        self.relay = gateway.RelayServer(
            ("127.0.0.1", 0), self.socket, max_body_bytes=65536, timeout_seconds=3
        )
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        self.assertEqual(self.request(relay=True)[0], 200)
        self.assertEqual(len(self.requests), 1)

    def test_missing_content_type_accepts_verified_responses_stream(self):
        self.upstream_content_type = None
        self.upstream_prefix = (
            b'event: response.created\r\n'
            b'data: {"type":"response.created","response":{"id":"resp_test","object":"response"}}\r\n\r\n'
        )
        status, raw = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(self.upstream_prefix))
        self.assertIn(b'[DONE]', raw)
        self.assertEqual(self.receipts[0]['outcome'], 'complete')
        self.assertEqual(self.receipts[0]['response_bytes'], len(raw))

    def test_missing_content_type_rejects_non_response_and_oversized_prefixes(self):
        self.upstream_content_type = None
        request_finished = threading.Event()

        def receipt(record):
            self.receipts.append(record)
            if record.get('event') == 'request':
                request_finished.set()

        self.broker.receipt = receipt
        for prefix in (b'<html>private-error</html>', b'data: {}\n\n',
                       b'event: response.created\ndata: not-json\n\n',
                       b'x' * (1024 * 1024 + 1)):
            with self.subTest(prefix=prefix[:40]):
                request_finished.clear()
                self.upstream_prefix = prefix
                status, raw = self.request()
                self.assertEqual(status, 502)
                self.assertNotIn(b'private-error', raw)
                # The error body arrives before upstream cleanup releases the
                # slot. Wait for its receipt so the next case reaches upstream
                # validation instead of correctly receiving a busy-slot 503.
                self.assertTrue(request_finished.wait(3), 'request did not finish cleanup')
        self.assertEqual(len(self.requests), 4)

    def test_explicit_wrong_content_type_is_rejected(self):
        self.upstream_content_type = 'application/json'
        self.assertEqual(self.request()[0], 502)

    def test_forbidden_request_shapes_never_reach_upstream(self):
        variants = [
            {**BODY, "model": "other"},
            {**BODY, "reasoning": {"effort": "medium"}},
            {**BODY, "tools": [{"type": "web_search"}]},
            {
                **BODY,
                "tools": [{"type": "mcp", "server_url": "http://example.invalid"}],
            },
            {
                **BODY,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": "http://example.invalid",
                            }
                        ],
                    }
                ],
            },
            {**BODY, "previous_response_id": "remote-state"},
            {**BODY, "background": True},
            {**BODY, "store": True},
            {**BODY, "stream": False},
        ]
        for body in variants:
            with self.subTest(body=body):
                self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(self.requests, [])

    def test_paths_and_framing_refused(self):
        for path in [
            "/responses?url=x",
            "http://example.invalid/responses",
            "/responses/../responses",
            "/responses/compact",
        ]:
            with self.subTest(path=path):
                self.assertNotEqual(self.request(path=path)[0], 200)
        for name, value in [
            ("Transfer-Encoding", "chunked"),
            ("Content-Encoding", "gzip"),
            ("Upgrade", "websocket"),
        ]:
            with self.subTest(name=name):
                self.assertEqual(self.request(headers={name: value})[0], 400)
        self.assertEqual(self.requests, [])

    def test_duplicate_headers_and_duplicate_json_keys_refused(self):
        for raw in [
            b"POST /responses HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\ncontent-length: 2\r\n\r\n{}",
            b'POST /responses HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 13\r\n\r\n{"x":1,"x":2}',
        ]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(3)
                s.connect(str(self.socket))
                s.sendall(raw)
                self.assertIn(b" 400 ", s.recv(4096).split(b"\r\n")[0])
        self.assertEqual(self.requests, [])

    def test_local_function_custom_tools_and_url_text_remain_valid(self):
        body = {
            **BODY,
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "format": {
                        "type": "grammar",
                        "syntax": "lark",
                        "definition": 'start: "patch"',
                    },
                },
            ],
            "input": [
                {
                    "role": "user",
                    "content": "source mentions https://example.invalid; this is ordinary text",
                },
                {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "c1",
                    "arguments": '{"command":"ls"}',
                },
                {"type": "function_call_output", "call_id": "c1", "output": "file.py"},
            ],
        }
        self.assertEqual(self.request(body)[0], 200)

    def test_budget_is_atomic_and_rejects_additional_upstream_calls(self):
        self.broker.config = gateway.GatewayConfig(
            model="gpt-5.6-terra",
            effort="xhigh",
            max_requests=1,
            max_body_bytes=65536,
            timeout_seconds=3,
        )
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 429)
        self.assertEqual(len(self.requests), 1)

    def test_configuration_and_auth_files_fail_closed(self):
        with self.assertRaises(ValueError):
            gateway.GatewayConfig.from_dict(
                {**self.config.as_dict(), "upstream": "http://evil.invalid"}
            )
        with self.assertRaises(ValueError):
            gateway.RelayServer(("0.0.0.0", 0), self.socket)
        auth = Path(self.tmp.name) / "auth.json"
        auth.write_text(
            json.dumps({"tokens": {"access_token": SECRET, "account_id": "a"}})
        )
        self.assertEqual(gateway.load_auth(auth).access_token, SECRET)
        link = Path(self.tmp.name) / "link"
        link.symlink_to(auth)
        with self.assertRaises(ValueError):
            gateway.load_auth(link)
        auth.write_text(
            json.dumps(
                {"tokens": {"access_token": SECRET + "\r\nBAD", "account_id": "a"}}
            )
        )
        with self.assertRaises(ValueError) as raised:
            gateway.load_auth(auth)
        self.assertNotIn(SECRET, str(raised.exception))

    def test_client_disconnect_interrupts_stalled_upstream(self):
        self.block_stream = True
        conn = UnixConnection(self.socket)
        conn.request(
            "POST", "/responses", json.dumps(BODY), {"Content-Type": "application/json"}
        )
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(self.upstream_waiting.wait(2))
        self.assertTrue(response.read1(5).startswith(b"data:"))
        # A stalled provider must be cancelled without waiting the request timeout.
        response.close()
        conn.close()
        self.assertTrue(self.upstream_closed.wait(1))

    def stream_receipt(self, body, *, close_early):
        self.block_stream = True
        self.upstream_body = body
        self.relay = gateway.RelayServer(
            ("127.0.0.1", 0), self.socket, max_body_bytes=65536, timeout_seconds=3
        )
        threading.Thread(target=self.relay.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection(*self.relay.server_address, timeout=3)
        conn.request("POST", "/responses", json.dumps(BODY), {"Content-Type": "application/json"})
        response = conn.getresponse()
        try:
            raw = response.read(len(body))
            self.assertEqual(raw, body)
            if not close_early:
                # Completion must not wait for the provider to close its socket.
                self.assertTrue(self.upstream_closed.wait(1))
                self.assertEqual(response.read(), b"")
        finally:
            response.close()
            conn.close()
        self.assertTrue(self.upstream_closed.wait(1))
        deadline = time.monotonic() + 1
        while not self.receipts and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(len(self.receipts), 1)
        return self.receipts[0]

    def test_terminal_event_finishes_without_waiting_for_provider_eof(self):
        body = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":11,"output_tokens":7}}}\n\n'
        receipt = self.stream_receipt(body, close_early=False)
        self.assertEqual(receipt['outcome'], 'complete')
        self.assertEqual(receipt['response_bytes'], len(body))
        self.assertEqual(receipt['usage'], {'input_tokens': 11, 'output_tokens': 7})

    def test_client_close_after_terminal_event_preserves_completion_and_usage(self):
        body = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":11,"output_tokens":7}}}\n\n'
        receipt = self.stream_receipt(body, close_early=True)
        self.assertEqual(receipt['outcome'], 'complete')
        self.assertEqual(receipt['response_bytes'], len(body))
        self.assertEqual(receipt['usage'], {'input_tokens': 11, 'output_tokens': 7})

    def test_client_close_before_completion_preserves_partial_byte_count(self):
        body = b'data: {"type":"response.created"}\n\n'
        receipt = self.stream_receipt(body, close_early=True)
        self.assertEqual(receipt['outcome'], 'client_disconnected')
        self.assertEqual(receipt['response_bytes'], len(body))
        self.assertEqual(receipt['usage'], {})

    def test_unterminated_terminal_event_is_not_completion(self):
        body = b'data: {"type":"response.completed"}\n'
        receipt = self.stream_receipt(body, close_early=True)
        self.assertEqual(receipt['outcome'], 'client_disconnected')
        self.assertEqual(receipt['response_bytes'], len(body))

    def test_eof_after_unterminated_terminal_event_is_incomplete(self):
        self.upstream_body = b'data: {"type":"response.completed"}\n'
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.receipts[0]['outcome'], 'upstream_incomplete')

    def test_fragmented_multiline_crlf_terminal_event_preserves_usage(self):
        body = (b'event: response.completed\r\ndata: {"type":"response.completed",\r\n'
                b'data: "response":{"padding":"' + b'x' * 20000 +
                b'","usage":{"input_tokens":11,"output_tokens":7}}}\r\n\r\n')
        receipt = self.stream_receipt(body, close_early=True)
        self.assertEqual(receipt['outcome'], 'complete')
        self.assertEqual(receipt['response_bytes'], len(body))
        self.assertEqual(receipt['usage'], {'input_tokens': 11, 'output_tokens': 7})

    def test_concurrent_limit_is_retryable_without_spending_request_budget(self):
        self.block_stream = True
        conn = UnixConnection(self.socket)
        conn.request(
            "POST", "/responses", json.dumps(BODY), {"Content-Type": "application/json"}
        )
        response = conn.getresponse()
        self.assertTrue(self.upstream_waiting.wait(2))
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.broker._request_count, 1)
        self.assertEqual(len(self.requests), 1)
        response.close()
        conn.close()
        self.assertTrue(self.upstream_closed.wait(1))
        deadline = time.monotonic() + 1
        while self.broker._active_requests and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.broker._active_requests, 0)
        self.block_stream = False
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.requests), 2)

    def test_additional_namespaced_tools_are_validated_recursively(self):
        tool = {
            "type": "namespace",
            "name": "functions",
            "tools": [
                {
                    "type": "function",
                    "name": "exec",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "additionalProperties": False,
                    },
                }
            ],
        }
        body = {
            **BODY,
            "reasoning": {"effort": "xhigh", "context": "all_turns"},
            "client_metadata": {"turn_id": "synthetic"},
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "id": "tools-1",
                    "tools": [tool],
                }
            ],
        }
        self.assertEqual(self.request(body)[0], 200)
        tool["tools"] = [{"type": "web_search"}]
        self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(len(self.requests), 1)

    def test_namespaced_local_tool_history_round_trip(self):
        for kind, payload in (("custom_tool_call", "input"), ("function_call", "arguments")):
            call = {"type": kind, "id": "tool_offline", "status": "completed",
                    "call_id": "call_offline", "name": "exec", "namespace": "functions",
                    payload: "synthetic local command"}
            tool = {"type": "custom" if kind == "custom_tool_call" else "function", "name": "exec"}
            if kind == "function_call":
                tool["parameters"] = {"type": "object", "properties": {}}
            body = {**BODY, "input": [
                {"type": "additional_tools", "role": "developer", "tools": [
                    {"type": "namespace", "name": "functions", "tools": [tool]}]},
                call,
                {"type": kind + "_output", "call_id": "call_offline", "output": [
                    {"type": "input_text", "text": "exit code 0"},
                    {"type": "input_text", "text": "synthetic output"}]},
            ]}
            self.assertEqual(self.request(body)[0], 200)
            for invalid in (None, 7, {}, "unknown", "https://example.invalid"):
                call["namespace"] = invalid
                self.assertEqual(self.request(body)[0], 400)
            call["namespace"] = "functions"
            call["name"] = "undeclared"
            self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(len(self.requests), 2)

    def test_unqualified_tool_history_requires_one_declared_local_match(self):
        tool = {"type": "custom", "name": "exec"}
        namespace = {"type": "namespace", "name": "functions", "tools": [tool]}
        call = {"type": "custom_tool_call", "call_id": "call_1", "name": "exec", "input": "local command"}
        body = {**BODY, "input": [
            {"type": "additional_tools", "role": "developer", "tools": [namespace]}, call]}
        self.assertEqual(self.request(body)[0], 200)
        body["input"][0]["tools"].append({**namespace, "name": "other"})
        self.assertEqual(self.request(body)[0], 400)
        body["input"][0]["tools"].pop()
        call["name"] = "undeclared"
        self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(len(self.requests), 1)

    def test_timeout_interrupts_stalled_response(self):
        self.block_stream = True
        self.broker.timeout_seconds = 0.3
        conn = UnixConnection(self.socket)
        conn.request(
            "POST", "/responses", json.dumps(BODY), {"Content-Type": "application/json"}
        )
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(self.upstream_waiting.wait(1))
        response.read()
        response.close()
        conn.close()
        self.assertTrue(self.upstream_closed.wait(1))
        deadline = time.monotonic() + 1
        while not self.receipts and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.receipts[0]["outcome"], "timeout")
        self.assertGreater(self.receipts[0]["started_at_unix_seconds"], 0)
        self.assertGreaterEqual(self.receipts[0]["elapsed_seconds"], .15)
        self.assertEqual(self.receipts[0]["timeout_seconds"], .3)

    def test_revocation_cancels_active_stream(self):
        self.block_stream = True
        conn = UnixConnection(self.socket)
        conn.request(
            "POST", "/responses", json.dumps(BODY), {"Content-Type": "application/json"}
        )
        response = conn.getresponse()
        self.assertTrue(self.upstream_waiting.wait(1))
        self.broker.stop()
        self.assertTrue(self.upstream_closed.wait(1))
        response.close()
        conn.close()
        self.assertFalse(self.socket.exists())

    def test_upstream_failures_do_not_expose_response_or_exception_secrets(self):
        self.upstream_status = 401
        status, body = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn(SECRET, body.decode())

        def fail(*_):
            raise OSError(SECRET)

        deadline = time.monotonic() + 1
        while (
            not any(r.get("event") == "request" for r in self.receipts)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.broker.transport = fail
        status, body = self.request()
        self.assertEqual(status, 502)
        self.assertNotIn(SECRET, body.decode())
        self.assertNotIn(SECRET, json.dumps(self.receipts))

    def test_production_transport_destination_and_tls_are_fixed(self):
        connection = mock.Mock()
        connection.getresponse.return_value.status = 200
        with mock.patch.object(
            gateway, "_CancellableHTTPSConnection", return_value=connection
        ) as factory:
            stream = gateway._production_transport(
                b"{}", {"Authorization": "Bearer fake"}, 2
            )
        self.assertEqual(factory.call_args.args, ("chatgpt.com",))
        self.assertTrue(factory.call_args.kwargs["context"].check_hostname)
        self.assertEqual(
            connection.request.call_args.args, ("POST", "/backend-api/codex/responses")
        )
        stream.close()

    def test_oversize_and_missing_length_and_methods_never_dispatch(self):
        for raw, code in [
            (
                b"POST /responses HTTP/1.1\r\nHost: x\r\nContent-Length: 65537\r\nContent-Type: application/json\r\n\r\n",
                413,
            ),
            (
                b"POST /responses HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\r\n",
                411,
            ),
            (b"CONNECT example.invalid:443 HTTP/1.1\r\nHost: x\r\n\r\n", 405),
        ]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                sock.connect(str(self.socket))
                sock.sendall(raw)
                self.assertIn(
                    (" " + str(code) + " ").encode(), sock.recv(4096).split(b"\r\n")[0]
                )
        self.assertEqual(self.requests, [])

    def test_partial_header_deadline_does_not_dispatch(self):
        self.broker.timeout_seconds = 0.2
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(str(self.socket))
            sock.sendall(b"POST /responses HTTP/1.1\r\nHost: x\r\n")
            # Absolute header timeout closes the connection without a provider call.
            self.assertEqual(sock.recv(4096), b"")
        self.assertEqual(self.requests, [])

    def test_broker_cli_reads_private_files_and_cleans_socket_on_sigterm(self):
        root = Path(self.tmp.name)
        socket_path = root / "cli.sock"
        auth = root / "cli-auth.json"
        config = root / "cli-config.json"
        auth.write_text(
            json.dumps({"tokens": {"access_token": SECRET, "account_id": "account"}})
        )
        config.write_text(json.dumps(self.config.as_dict()))
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "obench.sandbox_gateway",
                "broker",
                "--socket",
                str(socket_path),
                "--config",
                str(config),
                "--auth",
                str(auth),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(
                select.select([child.stdout], [], [], 5)[0],
                "broker did not report readiness",
            )
            self.assertEqual(
                json.loads(child.stdout.readline()),
                {"event": "ready", "role": "broker"},
            )
            connection = UnixConnection(socket_path)
            connection.request(
                "POST",
                "/responses",
                json.dumps({**BODY, "model": "wrong-model"}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.close()
            child.terminate()
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertNotIn(SECRET, stdout + stderr)
            self.assertFalse(socket_path.exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_receipt_failure_revokes_broker(self):
        def fail(_):
            raise OSError("private sink detail")

        self.broker.receipt = fail
        self.assertEqual(self.request()[0], 200)
        deadline = time.monotonic() + 2
        while self.socket.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.broker._receipt_failed)
        self.assertTrue(self.broker._revoked.is_set())
        self.assertFalse(self.socket.exists())

    def test_external_metaschema_and_reference_are_refused(self):
        for schema in (
            {"type": "object", "$schema": "https://example.invalid/schema"},
            {"$ref": "https://example.invalid/schema"},
        ):
            body = {
                **BODY,
                "tools": [{"type": "function", "name": "local", "parameters": schema}],
            }
            self.assertEqual(self.request(body)[0], 400)
        self.assertEqual(self.requests, [])

    def test_disconnect_cancels_production_transport_before_response_headers(self):
        self.stall_headers = True
        self.broker.transport = gateway._production_transport
        self.broker._production = True
        host, port = self.upstream.server_address

        class LocalConnection(gateway._CancellableHTTPSConnection):
            def __init__(self, requested_host, **kwargs):
                assert requested_host == "chatgpt.com"
                super().__init__(host, port=port, **kwargs)

            def connect(self):
                # Test-only cleartext loopback endpoint, using production's
                # registration/getresponse/cancellation path unchanged.
                if self.cancelled.is_set():
                    raise OSError("cancelled")
                http.client.HTTPConnection.connect(self)
                if self.cancelled.is_set():
                    self.cancel()
                    raise OSError("cancelled")

        with mock.patch.object(gateway, "_CancellableHTTPSConnection", LocalConnection):
            conn = UnixConnection(self.socket)
            conn.request(
                "POST",
                "/responses",
                json.dumps(BODY),
                {"Content-Type": "application/json"},
            )
            self.assertTrue(self.upstream_waiting.wait(1))
            conn.close()
            self.assertTrue(
                self.upstream_closed.wait(1),
                "pre-header provider call was not cancelled",
            )

    def test_sigterm_flushes_active_request_receipt_before_cli_exit(self):
        root = Path(self.tmp.name)
        socket_path = root / "active-cli.sock"
        auth = root / "active-auth.json"
        config = root / "active-config.json"
        auth.write_text(
            json.dumps({"tokens": {"access_token": SECRET, "account_id": "account"}})
        )
        config.write_text(json.dumps(self.config.as_dict()))
        self.block_stream = True
        # Python API substitution only; no CLI/config ability to change upstream.
        bootstrap = """
import http.client, sys
from obench import sandbox_gateway as g
port=int(sys.argv.pop(1))
class LocalConnection(g._CancellableHTTPSConnection):
    def __init__(self, requested_host, **kwargs):
        assert requested_host == 'chatgpt.com'
        super().__init__('127.0.0.1', port=port, **kwargs)
    def connect(self):
        if self.cancelled.is_set():
            raise OSError('cancelled')
        http.client.HTTPConnection.connect(self)
        self.observed_peer=g._socket_peer(self.sock)
        if self.cancelled.is_set():
            self.cancel()
            raise OSError('cancelled')
g._CancellableHTTPSConnection=LocalConnection
raise SystemExit(g.main())
"""
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                bootstrap,
                str(self.upstream.server_address[1]),
                "broker",
                "--socket",
                str(socket_path),
                "--config",
                str(config),
                "--auth",
                str(auth),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        connection = UnixConnection(socket_path)
        response = None
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0])
            self.assertEqual(
                json.loads(child.stdout.readline()),
                {"event": "ready", "role": "broker"},
            )
            connection.request(
                "POST",
                "/responses",
                json.dumps(BODY),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(self.upstream_waiting.wait(1))
            child.terminate()
            stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            records = [json.loads(line) for line in stdout.splitlines()]
            requests = [record for record in records if record["event"] == "request"]
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["outcome"], "revoked")
            self.assertEqual(
                requests[0]["upstream_peer"],
                {"ip": "127.0.0.1", "port": self.upstream.server_address[1]},
            )
            self.assertEqual(
                records[-1], {"event": "stopped", "role": "broker", "clean": True}
            )
            self.assertTrue(self.upstream_closed.wait(1))
            self.assertFalse(socket_path.exists())
            self.assertNotIn(SECRET, stdout + stderr)
        finally:
            if response is not None:
                response.close()
            connection.close()
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_stop_removes_socket(self):
        self.broker.stop()
        self.assertFalse(self.socket.exists())


if __name__ == "__main__":
    unittest.main()
