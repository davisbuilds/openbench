"""Real upstream bytes -> proxy -> raw client response controls."""

import gzip
import http.client
import socket
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from obench import proxy
from obench.spikes import counting_proxy_spike as spike
from experiments.ablations.tools import capture_proxy as capture


class RawUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.wfile.write(self.server.response)
        self.wfile.flush()
        if hasattr(self.server, "release_tail"):
            self.server.release_tail.wait(timeout=3)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args):
        pass


@contextmanager
def running(server):
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@contextmanager
def proxy_pair(kind, response, release_tail=None):
    with tempfile.TemporaryDirectory() as tmp:
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), RawUpstream)
        upstream.response = response
        if release_tail is not None:
            upstream.release_tail = release_tail
        target = f"http://127.0.0.1:{upstream.server_port}"
        with running(upstream):
            if kind == "counting":
                server = proxy.make_server("127.0.0.1", 0, tmp, openai_upstream=target)
                path = "/cell/header-test/openai/responses"
            elif kind == "spike":
                server = spike.SpikeProxy(("127.0.0.1", 0), spike.ProxyHandler)
                server.upstream = urlsplit(target)
                server.upstream_redacted = target
                server.timeout_s = 3
                server.capture_limit = 4096
                server.max_request_bytes = 4096
                server.log_path = Path(tmp) / "spike.jsonl"
                path = "/responses"
            else:
                server = capture.Server(("127.0.0.1", 0), capture.Handler, Path(tmp), target)
                path = "/responses"
            with running(server):
                yield server, path


def exchange(kind, response):
    with proxy_pair(kind, response) as (server, path):
        with socket.create_connection(server.server_address, timeout=3) as client:
            client.sendall(
                f"POST {path} HTTP/1.1\r\nHost: localhost\r\n"
                "Connection: close\r\nContent-Length: 2\r\n\r\n{}".encode()
            )
            chunks = []
            while chunk := client.recv(65536):
                chunks.append(chunk)
        return b"".join(chunks)


class ProxyResponseHeadersTests(unittest.TestCase):
    kinds = ("counting", "spike", "capture")

    def test_malformed_upstream_headers_return_one_clean_502(self):
        for bad in (b"X-Test: one\r\n folded: two", b"X-Test: one\n folded: two",
                    b"X-Test: one\x00two", b"X-Test: one\x7ftwo",
                    b"X@Bad: value", b"X Bad: value"):
            for kind in self.kinds:
                with self.subTest(kind=kind, bad=bad):
                    wire = exchange(kind, b"HTTP/1.1 200 OK\r\n" + bad
                                    + b"\r\nContent-Length: 8\r\n\r\nUPSTREAM")
                    self.assertIn(b" 502 ", wire.split(b"\r\n", 1)[0])
                    self.assertEqual(wire.count(b"HTTP/1."), 1)
                    self.assertNotIn(b"UPSTREAM", wire)
                    self.assertNotIn(b"X-Test:", wire)

    def test_malformed_reason_is_rejected_before_status_is_buffered(self):
        for kind in self.kinds:
            with self.subTest(kind=kind):
                wire = exchange(kind, b"HTTP/1.1 200 OK\rInjected: reason\r\n"
                                b"Content-Length: 0\r\n\r\n")
                self.assertIn(b" 502 ", wire.split(b"\r\n", 1)[0])
                self.assertEqual(wire.count(b"HTTP/1."), 1)
                self.assertNotIn(b"Injected", wire)

    def test_ambiguous_or_unsupported_framing_is_rejected(self):
        for framing in (b"Content-Length: 4\r\nContent-Length: 8",
                        b"Content-Length: 4\r\nTransfer-Encoding: chunked",
                        b"Content-Length: -1", b"Content-Length: invalid",
                        b"Content-Length: \xa02", b"Content-Length: 2\xa0",
                        b"Transfer-Encoding: gzip, chunked",
                        b"Transfer-Encoding: chunked "):
            for kind in self.kinds:
                with self.subTest(kind=kind, framing=framing):
                    wire = exchange(kind, b"HTTP/1.1 200 OK\r\n" + framing
                                    + b"\r\n\r\n0\r\n\r\n")
                    self.assertIn(b" 502 ", wire.split(b"\r\n", 1)[0])

    def test_valid_headers_duplicates_compression_and_error_status_survive(self):
        body = gzip.compress(b'{"error":"provider_limit"}')
        response = (b"HTTP/1.1 429 Provider Limit\r\nContent-Encoding: gzip\r\n"
                    b"Content-Type: application/json\r\nSet-Cookie: a=1\r\n"
                    b"Set-Cookie: b=2\r\nX-Valid: a\tb\xe9\r\nContent-Length: "
                    + str(len(body)).encode() + b"\r\n\r\n" + body)
        for kind in self.kinds:
            with self.subTest(kind=kind):
                head, actual = exchange(kind, response).split(b"\r\n\r\n", 1)
                self.assertIn(b" 429 Provider Limit", head)
                self.assertEqual(head.lower().count(b"content-length:"), 1)
                self.assertIn(b"Content-Encoding: gzip", head)
                self.assertEqual(head.count(b"Set-Cookie:"), 2)
                self.assertIn(b"X-Valid: a\tb\xe9", head)
                self.assertEqual(actual, body)

    def test_connection_nominated_headers_are_removed(self):
        response = (b"HTTP/1.1 200 OK\r\nConnection: X-Private\r\n"
                    b"Connection: X-Other, close\r\nX-Private: secret\r\n"
                    b"X-Other: secret\r\nX-End-To-End: yes\r\nContent-Length: 2\r\n\r\nOK")
        for kind in self.kinds:
            with self.subTest(kind=kind):
                head, body = exchange(kind, response).split(b"\r\n\r\n", 1)
                self.assertNotIn(b"X-Private:", head)
                self.assertNotIn(b"X-Other:", head)
                self.assertIn(b"X-End-To-End: yes", head)
                self.assertEqual(body, b"OK")

    def test_chunked_body_is_decoded_and_reframed_once(self):
        response = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                    b"Content-Type: text/event-stream\r\n\r\n"
                    b"5\r\nhello\r\n0\r\n\r\n")
        for kind in self.kinds:
            with self.subTest(kind=kind):
                head, body = exchange(kind, response).split(b"\r\n\r\n", 1)
                self.assertIn(b" 200 ", head)
                self.assertNotIn(b"transfer-encoding:", head.lower())
                self.assertEqual(body, b"hello")

    def test_identical_duplicate_lengths_are_collapsed(self):
        response = (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                    b"Content-Length: 2\r\n\r\nOK")
        for kind in self.kinds:
            with self.subTest(kind=kind):
                head, body = exchange(kind, response).split(b"\r\n\r\n", 1)
                self.assertIn(b" 200 ", head)
                self.assertEqual(head.lower().count(b"content-length:"), 1)
                self.assertEqual(body, b"OK")

    def test_counting_proxy_does_not_buffer_sse_until_upstream_eof(self):
        release = threading.Event()
        response = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                    b"Content-Type: text/event-stream\r\n\r\n5\r\nhello\r\n")
        with proxy_pair("counting", response, release) as (server, path):
            conn = http.client.HTTPConnection(*server.server_address, timeout=1)
            try:
                conn.request("POST", path, b"{}")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                # Upstream cannot finish until the client observes the first data.
                self.assertEqual(resp.read(5), b"hello")
                self.assertFalse(release.is_set())
                release.set()
                self.assertEqual(resp.read(), b"")
            finally:
                release.set()
                conn.close()


if __name__ == "__main__":
    unittest.main()
