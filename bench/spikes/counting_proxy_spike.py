#!/usr/bin/env python3
"""Minimal localhost pass-through proxy for the counting-proxy spike.

Forwards requests to one configured upstream base URL, preserving method/path/body
and end-to-end headers (including auth), while stripping hop-by-hop headers and
Host. Responses are streamed back byte-for-byte and a scrubbed JSONL record is
written per request with path, status, and provider usage parsed from JSON or SSE.
"""

from __future__ import annotations

import argparse
import gzip
import http.client
import json
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}
SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "anthropic-api-key"}


def redact(value: str | None) -> str | None:
    if value is None:
        return None
    return f"{value[:8]}…len={len(value)}"


def scrub_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        out[key] = redact(value) if key.lower() in SENSITIVE_HEADERS else value
    return out


def _looks_like_usage(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    token_keys = {
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    }
    return any(key in value for key in token_keys)


def extract_usage(obj: Any) -> Any:
    """Return the most likely provider usage object from a JSON value."""
    if isinstance(obj, dict):
        usage = obj.get("usage")
        if _looks_like_usage(usage):
            return usage
        # Some CLIs wrap the provider object under response/message/etc. Recurse
        # through the event and prefer the last nested usage-looking object.
        found = None
        for value in obj.values():
            candidate = extract_usage(value)
            if candidate is not None:
                found = candidate
        return found
    if isinstance(obj, list):
        found = None
        for value in obj:
            candidate = extract_usage(value)
            if candidate is not None:
                found = candidate
        return found
    return None


def decode_for_parsing(body: bytes, content_encoding: str) -> bytes:
    enc = content_encoding.lower().strip()
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            return zlib.decompress(body)
        if enc in {"zstd", "zst"}:
            import compression.zstd as zstd

            return zstd.decompress(body)
    except Exception:
        return body
    return body


def parse_sse_usage(body: bytes) -> Any:
    """Find the last SSE JSON event that carries a usage object."""
    text = body.decode("utf-8", "replace")
    # Normalize line endings, split on blank-line event boundaries.
    events = text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n")
    found = None
    for event in events:
        data_lines = []
        for line in event.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        usage = extract_usage(obj)
        if usage is not None:
            found = usage
    return found


def parse_json_usage(body: bytes) -> Any:
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return extract_usage(obj)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SpikeProxy/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # no default stderr request logs
        if getattr(self.server, "verbose", False):
            sys.stderr.write("proxy: " + (fmt % args) + "\n")

    def do_GET(self) -> None:  # noqa: N802
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy()

    def _proxy(self) -> None:
        upstream = self.server.upstream  # type: ignore[attr-defined]
        started = time.time()
        status = 502
        usage = None
        upstream_path = self._upstream_path(upstream)
        req_headers = self._forward_headers()
        resp_body = bytearray()
        capture_truncated = False
        headers_sent = False
        error = None
        try:
            body = self._read_body()
            conn_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
            port = upstream.port
            host = upstream.hostname
            if not host:
                raise RuntimeError("upstream URL missing host")
            conn = conn_cls(host, port=port, timeout=self.server.timeout_s)  # type: ignore[attr-defined]
            conn.request(self.command, upstream_path, body=body, headers=req_headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = resp.getheaders()
            self.send_response(resp.status, resp.reason)
            for key, value in resp_headers:
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            # We strip upstream Transfer-Encoding; force EOF framing for any
            # response without Content-Length (common for SSE/chunked streams).
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            headers_sent = True
            capture_limit = self.server.capture_limit  # type: ignore[attr-defined]
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if len(resp_body) + len(chunk) <= capture_limit:
                    resp_body.extend(chunk)
                else:
                    capture_truncated = True
                    keep = max(capture_limit - len(chunk), 0)
                    if keep:
                        resp_body = resp_body[-keep:]
                    resp_body.extend(chunk[-capture_limit:])
                self.wfile.write(chunk)
                self.wfile.flush()
            response_header_map = dict((k.lower(), v) for k, v in resp_headers)
            parse_body = decode_for_parsing(bytes(resp_body), response_header_map.get("content-encoding", ""))
            usage = parse_json_usage(parse_body)
            if usage is None:
                usage = parse_sse_usage(parse_body)
            conn.close()
        except Exception as exc:  # noqa: BLE001 - proxy spike should log exact failure
            error = f"{type(exc).__name__}: {exc}"
            if not headers_sent and not self.wfile.closed:
                payload = json.dumps({"error": "proxy_upstream_failed", "detail": str(exc)}).encode()
                try:
                    self.send_response(502)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception:
                    pass
            else:
                self.close_connection = True
        finally:
            self._write_record({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "method": self.command,
                "path": self.path,
                "upstream": self.server.upstream_redacted,  # type: ignore[attr-defined]
                "status": status,
                "usage": usage,
                "request_auth_headers": scrub_headers({k: v for k, v in self.headers.items() if k.lower() in SENSITIVE_HEADERS}),
                "duration_ms": round((time.time() - started) * 1000),
                **({"capture_truncated": True} if capture_truncated else {}),
                **({"error": error} if error else {}),
            })

    def _upstream_path(self, upstream: Any) -> str:
        base_path = upstream.path.rstrip("/")
        # self.path already includes query string.
        return f"{base_path}{self.path if self.path.startswith('/') else '/' + self.path}"

    def _forward_headers(self) -> dict[str, str]:
        connection_tokens = set()
        if self.headers.get("Connection"):
            connection_tokens = {x.strip().lower() for x in self.headers.get("Connection", "").split(",")}
        blocked = HOP_BY_HOP | connection_tokens
        return {k: v for k, v in self.headers.items() if k.lower() not in blocked}

    def _read_body(self) -> bytes:
        max_bytes = self.server.max_request_bytes  # type: ignore[attr-defined]
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            return self._read_chunked_body(max_bytes)
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError as exc:
            raise RuntimeError(f"invalid Content-Length: {length!r}") from exc
        if size < 0 or size > max_bytes:
            raise RuntimeError(f"request body too large: {size} > {max_bytes}")
        body = self.rfile.read(size)
        if len(body) != size:
            raise RuntimeError(f"incomplete request body: got {len(body)} of {size} bytes")
        return body

    def _read_chunked_body(self, max_bytes: int) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(128)
            if not line:
                raise RuntimeError("invalid chunked body: missing chunk size")
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError as exc:
                raise RuntimeError("invalid chunked body: bad chunk size") from exc
            if size == 0:
                # Consume trailers until the blank line.
                while True:
                    trailer = self.rfile.readline(8192)
                    if trailer in {b"\r\n", b"\n", b""}:
                        return bytes(body)
                
            if len(body) + size > max_bytes:
                raise RuntimeError(f"request body too large: chunked body > {max_bytes}")
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise RuntimeError("invalid chunked body: incomplete chunk")
            body.extend(chunk)
            crlf = self.rfile.read(2)
            if crlf not in {b"\r\n", b"\n"}:
                raise RuntimeError("invalid chunked body: missing chunk terminator")

    def _write_record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        log_path = self.server.log_path  # type: ignore[attr-defined]
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        print(line, end="", flush=True)


class SpikeProxy(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", required=True, help="Base URL, e.g. https://api.deepseek.com")
    parser.add_argument("--log", default=".worker/spike_proxy.jsonl")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--capture-limit", type=int, default=8 * 1024 * 1024, help="Max response bytes retained for usage parsing")
    parser.add_argument("--max-request-bytes", type=int, default=64 * 1024 * 1024, help="Max decoded request body bytes")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    upstream = urlsplit(args.upstream.rstrip("/"))
    if upstream.scheme not in {"http", "https"} or not upstream.netloc:
        parser.error("--upstream must be an http(s) base URL")
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    httpd = SpikeProxy((args.listen_host, args.port), ProxyHandler)
    httpd.upstream = upstream
    httpd.upstream_redacted = f"{upstream.scheme}://{upstream.netloc}{upstream.path.rstrip('/')}"
    httpd.log_path = log_path
    httpd.timeout_s = args.timeout
    httpd.capture_limit = max(args.capture_limit, 4096)
    httpd.max_request_bytes = max(args.max_request_bytes, 0)
    httpd.verbose = args.verbose
    print(f"listening=http://{args.listen_host}:{args.port} upstream={httpd.upstream_redacted} log={log_path}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
