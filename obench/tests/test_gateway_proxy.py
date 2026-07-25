#!/usr/bin/env python3
"""Lifecycle, durability, and compatibility tests for gateway proxy ledgers."""

import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from obench import proxy


SECRET = "GATEWAY_LEDGER_SECRET"


class BlockingUpstream(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class BlockingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        with self.server.request_count_lock:
            self.server.request_count += 1
        self.server.started.set()
        self.server.release.wait(5)
        payload = b'{"usage":{"prompt_tokens":3,"completion_tokens":2}}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class GatewayProxyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="router_proxy_test_")
        self.upstream = BlockingUpstream(("127.0.0.1", 0), BlockingHandler)
        self.upstream.started = threading.Event()
        self.upstream.release = threading.Event()
        self.upstream.request_count = 0
        self.upstream.request_count_lock = threading.Lock()
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        upstream_url = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.server = proxy.make_server(
            "127.0.0.1",
            0,
            self.tmp.name,
            chat_upstreams={"router": upstream_url},
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.upstream.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def _post(self, token, *, include_body=False):
        body = json.dumps({"model": "router-model", "api_key": SECRET}).encode()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request(
            "POST",
            f"/cell/{token}/chat/router/v1/chat/completions",
            body=body,
            headers={"content-type": "application/json", "content-length": str(len(body))},
        )
        response = conn.getresponse()
        response_body = response.read()
        conn.close()
        return (response.status, response_body) if include_body else response.status

    def _rows(self, token):
        path = self.server.ledger_dir / f"{token}.jsonl"
        deadline = time.monotonic() + 2
        while (not path.exists() or not path.stat().st_size) and time.monotonic() < deadline:
            time.sleep(0.01)
        with path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def test_registered_cell_drains_seals_and_rejects_new_or_late_writes(self):
        token = "gateway-cell"
        self.server.register_cell(token)
        statuses = []
        request = threading.Thread(target=lambda: statuses.append(self._post(token)))
        request.start()
        self.assertTrue(self.upstream.started.wait(2))

        self.server.revoke_cell(token)
        self.assertEqual(self._post(token), 502)
        with self.assertRaises(TimeoutError):
            self.server.seal_cell(token, timeout_s=0.01)

        self.upstream.release.set()
        request.join(2)
        self.assertEqual(statuses, [200])
        seal = self.server.seal_cell(token, timeout_s=1)
        self.assertIsInstance(seal, proxy.LedgerSeal)
        self.assertEqual(seal.record_count, 1)
        self.assertEqual(seal.last_sequence, 1)

        rows = self._rows(token)
        self.assertEqual([row["record_type"] for row in rows], ["request", "ledger_seal"])
        request_row, terminal = rows
        self.assertEqual(request_row["previous_hash"], proxy.EMPTY_LEDGER_HASH)
        unhashed = {key: value for key, value in request_row.items() if key != "record_hash"}
        expected_hash = hashlib.sha256(
            json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(request_row["record_hash"], expected_hash)
        self.assertEqual(seal.root_hash, expected_hash)
        self.assertEqual(terminal["root_hash"], expected_hash)
        self.assertEqual(terminal["record_count"], 1)
        self.assertEqual(terminal["last_sequence"], 1)
        self.assertNotIn(SECRET, seal.path.read_text(encoding="utf-8"))

        self.assertEqual(self._post(token), 502)
        with self.assertRaisesRegex(RuntimeError, "sealed"):
            self.server.complete_cell_request(token, {"status": 200})
        self.assertEqual(self.server.seal_cell(token), seal)
        self.assertEqual(len(self._rows(token)), 2)

    def test_concurrent_completions_are_sequenced_and_hash_chained(self):
        token = "concurrent-cell"
        self.server.register_cell(token)
        self.upstream.release.set()
        threads = [threading.Thread(target=self._post, args=(token,)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())

        seal = self.server.seal_cell(token, timeout_s=1)
        rows = self._rows(token)
        requests = rows[:-1]
        self.assertEqual([row["sequence"] for row in requests], list(range(1, 9)))
        previous = proxy.EMPTY_LEDGER_HASH
        for row in requests:
            self.assertEqual(row["previous_hash"], previous)
            previous = row["record_hash"]
        self.assertEqual(seal.record_count, 8)
        self.assertEqual(seal.root_hash, previous)

    def test_max_calls_is_reserved_atomically_before_upstream_forwarding(self):
        token = "capped-cell"
        max_calls = 8
        self.server.register_cell(token, max_calls=max_calls)
        self.upstream.request_count = 0
        self.upstream.release.set()
        start = threading.Barrier(25)
        responses = []

        def post():
            start.wait()
            responses.append(self._post(token, include_body=True))

        threads = [threading.Thread(target=post) for _ in range(24)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())

        seal = self.server.seal_cell(token, timeout_s=1)
        rows = self._rows(token)
        requests = rows[:-1]
        statuses = [status for status, _body in responses]
        self.assertEqual(self.upstream.request_count, max_calls)
        self.assertEqual(statuses.count(200), max_calls)
        self.assertEqual(statuses.count(429), len(threads) - max_calls)
        rejection_bodies = {
            body for status, body in responses if status == 429
        }
        self.assertEqual(rejection_bodies, {
            b'{"error":{"code":"max_calls_exceeded",'
            b'"message":"OpenBench max_calls budget exceeded",'
            b'"type":"openbench_budget_error"}}'
        })
        self.assertEqual(seal.record_count, max_calls + 1)
        self.assertEqual([row["status"] for row in requests].count(429), 1)
        rejection = next(row for row in requests if row["status"] == 429)
        self.assertEqual(rejection["error"], "max_calls_exceeded")
        self.assertNotIn(SECRET, seal.path.read_text(encoding="utf-8"))

    def test_exhausted_cell_returns_budget_error_while_draining_and_after_seal(self):
        token = "draining-capped-cell"
        self.server.register_cell(token, max_calls=1)
        statuses = []
        request = threading.Thread(target=lambda: statuses.append(self._post(token)))
        request.start()
        self.assertTrue(self.upstream.started.wait(2))

        seals = []
        sealing = threading.Thread(
            target=lambda: seals.append(self.server.seal_cell(token, timeout_s=2))
        )
        sealing.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with self.server._ledger_condition:
                if self.server._cell_ledgers[token].state == "DRAINING":
                    break
            time.sleep(0.01)
        else:
            self.fail("cell did not begin draining")

        status, body = self._post(token, include_body=True)
        self.assertEqual(status, 429)
        self.assertIn(b'"code":"max_calls_exceeded"', body)
        self.assertEqual(self.upstream.request_count, 1)

        self.upstream.release.set()
        request.join(2)
        sealing.join(2)
        self.assertEqual(statuses, [200])
        self.assertEqual(len(seals), 1)
        self.assertEqual(seals[0].record_count, 2)

        status, body = self._post(token, include_body=True)
        self.assertEqual(status, 429)
        self.assertIn(b'"code":"max_calls_exceeded"', body)
        self.assertEqual(self.upstream.request_count, 1)
        self.assertEqual(self.server.seal_cell(token).record_count, 2)

    def test_unregistered_legacy_cell_keeps_plain_append_behavior(self):
        self.upstream.release.set()
        self.assertEqual(self._post("legacy-cell"), 200)
        row = self._rows("legacy-cell")[0]
        self.assertNotIn("record_type", row)
        self.assertNotIn("sequence", row)
        self.assertNotIn("record_hash", row)
        with self.assertRaisesRegex(RuntimeError, "not registered"):
            self.server.seal_cell("legacy-cell", timeout_s=0)

    def test_registration_rejects_request_already_admitted_as_legacy(self):
        token = "registration-race"
        self.assertFalse(self.server.admit_cell_request(token))
        with self.assertRaisesRegex(RuntimeError, "active legacy"):
            self.server.register_cell(token)
        self.server.complete_legacy_request(token, {"status": 200, "api_key": SECRET})
        row = self._rows(token)[0]
        self.assertEqual(row["status"], 200)
        self.assertNotIn(SECRET, self.server._ledger_path(token).read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            self.server.register_cell(token)

    def test_seal_retry_does_not_append_duplicate_terminal_record(self):
        token = "seal-retry"
        self.server.register_cell(token)
        original_fsync_directory = self.server._fsync_directory
        calls = 0

        def fail_once(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected directory fsync failure")
            return original_fsync_directory(path)

        with mock.patch.object(self.server, "_fsync_directory", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "injected"):
                self.server.seal_cell(token)
            seal = self.server.seal_cell(token)

        rows = self._rows(token)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_type"], "ledger_seal")
        self.assertEqual(seal.record_count, 0)

    def test_legacy_header_token_is_sanitized_before_path_use(self):
        self.upstream.release.set()
        token = "../../outside"
        body = b'{"model":"router-model"}'
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request(
            "POST",
            "/chat/router/v1/chat/completions",
            body=body,
            headers={
                "x-openbench-cell-token": token,
                "content-type": "application/json",
                "content-length": str(len(body)),
            },
        )
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        safe_path = self.server.ledger_dir / ".._.._outside.jsonl"
        deadline = time.monotonic() + 2
        while not safe_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(safe_path.exists())
        self.assertFalse((self.server.ledger_dir.parent.parent / "outside.jsonl").exists())

    def test_pre_admission_error_cannot_write_after_registration(self):
        token = "managed-error"
        self.server.register_cell(token)
        wrote = self.server.write_legacy_record_if_unregistered(token, {"error": "late"})
        self.assertFalse(wrote)
        seal = self.server.seal_cell(token)
        rows = self._rows(token)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["record_type"], "ledger_seal")
        self.assertEqual(seal.record_count, 0)


if __name__ == "__main__":
    unittest.main()
