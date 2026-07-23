#!/usr/bin/env python3
"""Transparent counting proxy for OpenBench cells.

Stdlib-only HTTP pass-through proxy. Routing is path based and intentionally
simple: callers address ``/cell/<token>/<route>/...`` and this proxy strips the
cell and route prefix before forwarding to the configured upstream. Each model
call writes one scrubbed JSONL ledger row under ``<ledger-dir>/<token>.jsonl``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.client
import json
import os
import re
import secrets
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
}
SENSITIVE_HEADERS = {
    "authorization", "x-api-key", "api-key", "anthropic-api-key", "cookie",
    "set-cookie", "openai-api-key",
}
SENSITIVE_KEYS = {
    "authorization", "x-api-key", "api-key", "anthropic-api-key", "api_key",
    "apikey", "apiKey", "key", "token", "access_token", "refresh_token",
    "id_token", "cookie", "password", "secret",
}
SAMPLING_KEYS = {
    "model", "temperature", "top_p", "top_k", "max_tokens",
    "max_completion_tokens", "max_output_tokens", "reasoning_effort",
    "reasoning", "thinking", "stream", "stream_options", "stop", "seed",
}
TOKEN_RE = re.compile(r"/cell/([^/?#]+)")

DEFAULT_CURSOR_UPSTREAM = "https://api2.cursor.sh"
DEFAULT_CHAT_UPSTREAMS = {
    "deepseek": "https://api.deepseek.com",
    "zai": "https://api.z.ai",
    "moonshot": "https://api.moonshot.ai",
    "openrouter": "https://openrouter.ai",
}
DEFAULT_ANTHROPIC_UPSTREAMS = {
    "default": "https://api.anthropic.com",
    "anthropic": "https://api.anthropic.com",
    "deepseek": "https://api.deepseek.com",
    "zai": "https://api.z.ai",
    "moonshot": "https://api.moonshot.ai",
    # Local CLIProxyAPI Anthropic-compatible ingress (claude x gpt-5.6-sol).
    "cliproxyapi": "http://127.0.0.1:8317",
}
EMPTY_LEDGER_HASH = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class LedgerSeal:
    """Durable terminal state for one explicitly registered cell ledger."""

    token: str
    path: Path
    record_count: int
    last_sequence: int
    root_hash: str

    @property
    def ledger_path(self) -> Path:
        return self.path


@dataclass
class _CellLedger:
    state: str = "ACTIVE"
    in_flight: int = 0
    record_count: int = 0
    last_sequence: int = 0
    root_hash: str = EMPTY_LEDGER_HASH
    seal: LedgerSeal | None = None
    write_error: str | None = None
    terminal_written: bool = False


def new_cell_token() -> str:
    return secrets.token_urlsafe(18)


def cell_url(base_url: str, token: str, *parts: str) -> str:
    quoted = [quote(str(p).strip("/"), safe="") for p in ("cell", token, *parts) if str(p).strip("/")]
    return base_url.rstrip("/") + "/" + "/".join(quoted)


def redact(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    return f"<REDACTED len={len(text)}>"


def _sensitive_name(name: str) -> bool:
    low = name.lower().replace("-", "_")
    sensitive = {k.lower().replace("-", "_") for k in SENSITIVE_KEYS}
    if low in sensitive:
        return True
    if low.endswith("_tokens") or low.endswith("tokens"):
        return False
    # Redact credential-like fields without catching accounting fields such as
    # input_tokens / completion_tokens / totalTokens.
    return any(part in low for part in ("secret", "password")) or low.endswith("_token") or low.endswith("token")


def scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            out[key] = redact(value) if _sensitive_name(str(key)) else scrub(value)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def _looks_like_usage(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {
        "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
        "total_tokens", "cached_input_tokens", "reasoning_output_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens", "totalTokens",
        "cacheRead", "cacheWrite", "input", "output",
    }
    return any(k in value for k in keys)


def extract_usage(obj: Any) -> Any:
    """Return the last nested usage-looking object in a provider response."""
    if isinstance(obj, dict):
        usage = obj.get("usage")
        if _looks_like_usage(usage):
            found = usage
        elif _looks_like_usage(obj):
            found = obj
        else:
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
    enc = (content_encoding or "").lower().strip()
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            return zlib.decompress(body)
    except Exception:
        return body
    return body


def parse_json_usage(body: bytes) -> Any:
    try:
        return extract_usage(json.loads(body.decode("utf-8", "replace")))
    except json.JSONDecodeError:
        return None


def parse_sse_usage(body: bytes) -> Any:
    text = body.decode("utf-8", "replace")
    found = None
    for event in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
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


def _collect_sampling(obj: Any, out: dict[str, Any], depth: int = 0) -> None:
    """Collect request sampling hints without recording prompts/tool payloads."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in SAMPLING_KEYS and key not in out:
                out[key] = value
            elif key in {"model_slug", "modelSlug"} and "model" not in out:
                out["model"] = value
        for value in obj.values():
            _collect_sampling(value, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _collect_sampling(value, out, depth + 1)


def _json_objects(body: bytes) -> list[Any]:
    """Decode a JSON body or JSON objects carried in SSE data lines."""
    text = body.decode("utf-8", "replace")
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        objects = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                objects.append(json.loads(data))
            except json.JSONDecodeError:
                continue
        return objects


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def protocol_links(body: bytes, *, response: bool = False) -> dict[str, str]:
    """Extract opaque conversation links; callers hash them before persistence."""
    found = {}
    for obj in _json_objects(body):
        for item in _walk_dicts(obj):
            if not response:
                previous = item.get("previous_response_id")
                if isinstance(previous, str):
                    found.setdefault("previous_response", previous)
                for key in ("session_id", "conversation_id"):
                    value = item.get(key)
                    if isinstance(value, str):
                        found.setdefault("session", value)
                conversation = item.get("conversation")
                if isinstance(conversation, str):
                    found.setdefault("session", conversation)
                elif isinstance(conversation, dict) and isinstance(conversation.get("id"), str):
                    found.setdefault("session", conversation["id"])
            candidate = item.get("response")
            if response and isinstance(candidate, dict) and isinstance(candidate.get("id"), str):
                found["response"] = candidate["id"]
            if response and isinstance(item.get("id"), str):
                kind = str(item.get("object") or item.get("type") or "")
                if kind == "response" or kind.startswith("response."):
                    found["response"] = item["id"]
    return found


def _identifier_hash(value: str, salt: bytes) -> str:
    return hashlib.sha256(salt + value.encode("utf-8", "replace")).hexdigest()


def observed_sampling(body: bytes, content_type: str = "", content_encoding: str = "") -> dict[str, Any]:
    if not body:
        return {}
    parse_body = decode_for_parsing(body, content_encoding)
    stripped = parse_body.lstrip()
    if content_type and "json" not in content_type.lower() and not stripped.startswith((b"{", b"[")):
        return {}
    try:
        obj = json.loads(parse_body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {}
    sampling: dict[str, Any] = {}
    _collect_sampling(obj, sampling)
    return scrub(sampling)


def _urlsplit_map(values: dict[str, str]) -> dict[str, Any]:
    out = {}
    for name, url in values.items():
        parsed = urlsplit(url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"bad upstream URL for {name}: {url!r}")
        out[name] = parsed
    return out


class Route:
    def __init__(self, token: str, route: str, upstream: Any, upstream_path: str):
        self.token = token
        self.route = route
        self.upstream = upstream
        self.upstream_path = upstream_path


class CountingProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenBenchCountingProxy/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "verbose", False):
            sys.stderr.write("proxy: " + (fmt % args) + "\n")

    def do_GET(self) -> None: self._proxy()  # noqa: N802,E704
    def do_POST(self) -> None: self._proxy()  # noqa: N802,E704
    def do_PUT(self) -> None: self._proxy()  # noqa: N802,E704
    def do_PATCH(self) -> None: self._proxy()  # noqa: N802,E704
    def do_DELETE(self) -> None: self._proxy()  # noqa: N802,E704
    def do_OPTIONS(self) -> None: self._proxy()  # noqa: N802,E704

    def _proxy(self) -> None:
        started = time.time()
        status = 502
        usage = None
        body = b""
        sampling: dict[str, Any] = {}
        links: dict[str, str] = {}
        route = None
        capture_truncated = False
        headers_sent = False
        error = None
        token = self._token_from_header_or_path()
        managed_ledger: bool | None = None
        try:
            route = self._route_request()
            token = route.token
            managed_ledger = self.server.admit_cell_request(token)  # type: ignore[attr-defined]
            body = self._read_body()
            request_body = decode_for_parsing(body, self.headers.get("content-encoding", ""))
            sampling = observed_sampling(
                body,
                self.headers.get("content-type", ""),
                self.headers.get("content-encoding", ""),
            )
            links.update(protocol_links(request_body))
            headers = self._forward_headers()
            conn_cls = http.client.HTTPSConnection if route.upstream.scheme == "https" else http.client.HTTPConnection
            if not route.upstream.hostname:
                raise RuntimeError("upstream URL missing host")
            conn = conn_cls(route.upstream.hostname, port=route.upstream.port, timeout=self.server.timeout_s)  # type: ignore[attr-defined]
            conn.request(self.command, route.upstream_path, body=body, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = resp.getheaders()
            self.send_response(resp.status, resp.reason)
            for key, value in resp_headers:
                if key.lower() in HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            headers_sent = True
            capture = bytearray()
            limit = self.server.capture_limit  # type: ignore[attr-defined]
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                if len(capture) + len(chunk) <= limit:
                    capture.extend(chunk)
                else:
                    capture_truncated = True
                    keep = max(limit - len(chunk), 0)
                    if keep:
                        capture = capture[-keep:]
                    capture.extend(chunk[-limit:])
                self.wfile.write(chunk)
                self.wfile.flush()
            header_map = {k.lower(): v for k, v in resp_headers}
            parse_body = decode_for_parsing(bytes(capture), header_map.get("content-encoding", ""))
            usage = parse_json_usage(parse_body)
            if usage is None:
                usage = parse_sse_usage(parse_body)
            links.update(protocol_links(parse_body, response=True))
            conn.close()
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            if not headers_sent and not self.wfile.closed:
                payload = json.dumps({"error": "proxy_upstream_failed"}).encode()
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
            token = route.token if route is not None else token
            meta = self._read_metadata(token)
            configured_sampling = meta.get("sampling") if isinstance(meta.get("sampling"), dict) else {}
            recorded_sampling = sampling or configured_sampling
            rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "method": self.command,
                "path": self._scrub_cell_path(self.path),
                "status": status,
                "usage": scrub(usage),
                "model": sampling.get("model") or configured_sampling.get("model"),
                "sampling_observed": recorded_sampling,
                "sampling_source": "http_request" if sampling else (meta.get("source") if recorded_sampling else None),
                "duration_ms": round((time.time() - started) * 1000),
            }
            if route is not None:
                rec["route"] = route.route
                rec["upstream"] = f"{route.upstream.scheme}://{route.upstream.netloc}{route.upstream.path.rstrip('/')}"
            if "session" in links:
                rec["session_hash"] = _identifier_hash(links["session"], self.server.identifier_salt)
            if "previous_response" in links:
                rec["previous_response_hash"] = _identifier_hash(
                    links["previous_response"], self.server.identifier_salt)
            if "response" in links:
                rec["response_hash"] = _identifier_hash(links["response"], self.server.identifier_salt)
            if capture_truncated:
                rec["capture_truncated"] = True
            if error:
                rec["error"] = error
            if managed_ledger:
                self.server.complete_cell_request(token, rec)  # type: ignore[attr-defined]
            elif managed_ledger is False:
                self.server.complete_legacy_request(token, rec)  # type: ignore[attr-defined]
            else:
                self.server.write_legacy_record_if_unregistered(token, rec)  # type: ignore[attr-defined]

    def _route_request(self) -> Route:
        path_query = self.path if self.path.startswith("/") else "/" + self.path
        path = path_query.split("?", 1)[0]
        query = "?" + path_query.split("?", 1)[1] if "?" in path_query else ""
        parts = [p for p in path.split("/") if p]
        token = self.headers.get("x-openbench-cell-token")
        if len(parts) >= 3 and parts[0] == "cell":
            token = parts[1]
            route_parts = parts[2:]
        elif token:
            route_parts = parts
        else:
            raise RuntimeError("missing /cell/<token>/ route or x-openbench-cell-token")
        if not token:
            raise RuntimeError("empty cell token")
        registration_meta = None
        if getattr(self.server, "require_registered_tokens", False):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
                raise RuntimeError("unregistered cell token")
            registration = Path(self.server.ledger_dir) / f"{token}.meta.json"
            try:
                registration_meta = json.loads(registration.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise RuntimeError("unregistered cell token") from None
        if not route_parts:
            raise RuntimeError("missing route prefix")
        prefix = route_parts[0]
        tail = route_parts[1:]
        if prefix == "subbridge" and registration_meta is not None:
            if (registration_meta.get("harness") != "grokbuild"
                    or registration_meta.get("model") not in {"gpt-5.6", "gpt-5.6-sol"}):
                raise RuntimeError("cell token is not authorized for subscription bridge")
        if prefix == "codex":
            upstream = self.server.upstreams["codex"]  # type: ignore[attr-defined]
            route_name = "codex"
        elif prefix == "openai":
            upstream = self.server.upstreams["openai"]  # type: ignore[attr-defined]
            route_name = "openai"
        elif prefix == "bridge":
            # Local LiteLLM open-model bridge (bench/openmodel_bridge.sh):
            # codex speaks Responses to it; usage extraction is the same
            # SSE/JSON parsing used on every other route.
            upstream = self.server.upstreams["bridge"]  # type: ignore[attr-defined]
            route_name = "bridge"
        elif prefix == "subbridge":
            # Local CLIProxyAPI subscription bridge (Codex/ChatGPT OAuth).
            upstream = self.server.upstreams["subbridge"]  # type: ignore[attr-defined]
            route_name = "subbridge"
        elif prefix == "cursor":
            # Cursor Agent's endpoint speaks Cursor's private HTTP/Connect-RPC
            # protocol rather than a public model-provider dialect. Forward it
            # byte-for-byte; usage extraction remains best-effort for JSON/SSE.
            upstream = self.server.upstreams["cursor"]  # type: ignore[attr-defined]
            route_name = "cursor"
        elif prefix == "anthropic":
            name = tail[0] if tail and tail[0] in self.server.anthropic_upstreams else "default"  # type: ignore[attr-defined]
            if name != "default":
                tail = tail[1:]
            upstream = self.server.anthropic_upstreams[name]  # type: ignore[attr-defined]
            route_name = f"anthropic/{name}"
        elif prefix == "chat":
            if not tail:
                raise RuntimeError("/chat route requires a vendor segment")
            name = tail[0]
            if name not in self.server.chat_upstreams:  # type: ignore[attr-defined]
                raise RuntimeError(f"unknown chat vendor: {name}")
            tail = tail[1:]
            upstream = self.server.chat_upstreams[name]  # type: ignore[attr-defined]
            route_name = f"chat/{name}"
        else:
            raise RuntimeError(f"unknown route prefix: {prefix}")
        stripped = "/" + "/".join(tail) if tail else "/"
        base = upstream.path.rstrip("/")
        upstream_path = (base + stripped if base else stripped) + query
        return Route(token, route_name, upstream, upstream_path)

    def _token_from_header_or_path(self) -> str:
        token = self.headers.get("x-openbench-cell-token")
        if token:
            return token
        match = TOKEN_RE.search(self.path)
        return match.group(1) if match else "unknown"

    def _read_metadata(self, token: str) -> dict[str, Any]:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", token or "unknown")
        path = Path(self.server.ledger_dir) / f"{safe}.meta.json"  # type: ignore[attr-defined]
        try:
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _scrub_cell_path(path: str) -> str:
        return TOKEN_RE.sub("/cell/<redacted>", path)

    def _forward_headers(self) -> dict[str, str]:
        connection_tokens = set()
        if self.headers.get("Connection"):
            connection_tokens = {x.strip().lower() for x in self.headers.get("Connection", "").split(",")}
        # Host identifies this proxy on ingress; let http.client generate the
        # selected upstream's Host header instead of forwarding the proxy host.
        blocked = HOP_BY_HOP | connection_tokens | {"host", "x-openbench-cell-token"}
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

    def _write_record(self, token: str, record: dict[str, Any]) -> None:
        ledger_dir = self.server.ledger_dir  # type: ignore[attr-defined]
        ledger_dir.mkdir(parents=True, exist_ok=True)
        safe_token = re.sub(r"[^A-Za-z0-9_.-]", "_", token or "unknown")
        path = ledger_dir / f"{safe_token}.jsonl"
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        if getattr(self.server, "verbose", False):
            print(line, end="", flush=True)


class CountingProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def register_cell(self, token: str) -> None:
        """Opt a router cell into lifecycle-managed, sealed ledger writes."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
            raise ValueError("cell token must contain only letters, digits, '.', '_', or '-'")
        with self._ledger_condition:
            if token in self._cell_ledgers:
                raise RuntimeError(f"cell already registered: {token}")
            if self._legacy_in_flight.get(token, 0):
                raise RuntimeError(f"cell has active legacy requests: {token}")
            path = self._ledger_path(token)
            if path.exists():
                raise FileExistsError(f"cell ledger already exists: {path}")
            self._cell_ledgers[token] = _CellLedger()

    def cell_is_registered(self, token: str) -> bool:
        with self._ledger_condition:
            return token in self._cell_ledgers

    def admit_cell_request(self, token: str) -> bool:
        """Atomically admit a request, returning False for a legacy cell."""
        with self._ledger_condition:
            ledger = self._cell_ledgers.get(token)
            if ledger is None:
                self._legacy_in_flight[token] = self._legacy_in_flight.get(token, 0) + 1
                return False
            if ledger.state != "ACTIVE":
                raise RuntimeError(f"cell ledger is {ledger.state.lower()}: {token}")
            ledger.in_flight += 1
            return True

    def complete_cell_request(self, token: str, record: dict[str, Any]) -> None:
        """Durably append one admitted request and release its in-flight slot."""
        with self._ledger_condition:
            ledger = self._cell_ledgers.get(token)
            if ledger is None:
                raise RuntimeError(f"cell is not registered: {token}")
            if ledger.state == "SEALED":
                raise RuntimeError(f"cell ledger is sealed: {token}")
            if ledger.in_flight <= 0:
                raise RuntimeError(f"cell has no admitted request: {token}")
            try:
                sequence = ledger.last_sequence + 1
                chained = scrub(record)
                for key in ("record_type", "sequence", "previous_hash", "record_hash"):
                    chained.pop(key, None)
                chained.update({
                    "record_type": "request",
                    "sequence": sequence,
                    "previous_hash": ledger.root_hash,
                })
                canonical = json.dumps(chained, sort_keys=True, separators=(",", ":"))
                record_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                chained["record_hash"] = record_hash
                self._append_durable(self._ledger_path(token), chained)
                ledger.record_count += 1
                ledger.last_sequence = sequence
                ledger.root_hash = record_hash
            except Exception as exc:
                ledger.state = "DRAINING"
                ledger.write_error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                ledger.in_flight -= 1
                self._ledger_condition.notify_all()

    def complete_legacy_request(self, token: str, record: dict[str, Any]) -> None:
        """Append a request admitted before explicit lifecycle registration."""
        with self._ledger_condition:
            in_flight = self._legacy_in_flight.get(token, 0)
            if in_flight <= 0:
                raise RuntimeError(f"cell has no admitted legacy request: {token}")
            try:
                self._append_legacy(self._legacy_ledger_path(token), scrub(record))
            finally:
                if in_flight == 1:
                    self._legacy_in_flight.pop(token, None)
                else:
                    self._legacy_in_flight[token] = in_flight - 1
                self._ledger_condition.notify_all()

    def write_legacy_record_if_unregistered(
            self, token: str, record: dict[str, Any]) -> bool:
        """Atomically retain a pre-admission error unless the cell is managed."""
        with self._ledger_condition:
            if token in self._cell_ledgers:
                return False
            self._append_legacy(self._legacy_ledger_path(token), scrub(record))
            return True

    def revoke_cell(self, token: str) -> None:
        """Stop new admissions while allowing admitted requests to drain."""
        with self._ledger_condition:
            ledger = self._require_cell(token)
            if ledger.state == "SEALED":
                raise RuntimeError(f"cell ledger is sealed: {token}")
            ledger.state = "DRAINING"

    def seal_cell(self, token: str, timeout_s: float = 30.0) -> LedgerSeal:
        """Drain a cell for at most ``timeout_s`` and durably seal its ledger."""
        if timeout_s < 0:
            raise ValueError("timeout_s must be nonnegative")
        deadline = time.monotonic() + timeout_s
        with self._ledger_condition:
            ledger = self._require_cell(token)
            if ledger.seal is not None:
                return ledger.seal
            ledger.state = "DRAINING"
            while ledger.in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out draining cell {token}: {ledger.in_flight} request(s) remain")
                self._ledger_condition.wait(remaining)
            if ledger.write_error is not None:
                raise RuntimeError(
                    f"cannot seal cell {token} after ledger write failure: {ledger.write_error}")

            path = self._ledger_path(token)
            terminal = {
                "record_type": "ledger_seal",
                "state": "SEALED",
                "record_count": ledger.record_count,
                "last_sequence": ledger.last_sequence,
                "root_hash": ledger.root_hash,
            }
            if not ledger.terminal_written:
                try:
                    self._append_durable(path, terminal)
                except Exception:
                    if self._last_record_equals(path, terminal):
                        ledger.terminal_written = True
                    else:
                        self._truncate_partial_record(path)
                    raise
                ledger.terminal_written = True
            else:
                self._fsync_file(path)
            self._fsync_directory(path.parent)
            ledger.state = "SEALED"
            ledger.seal = LedgerSeal(
                token=token,
                path=path,
                record_count=ledger.record_count,
                last_sequence=ledger.last_sequence,
                root_hash=ledger.root_hash,
            )
            return ledger.seal

    def _require_cell(self, token: str) -> _CellLedger:
        ledger = self._cell_ledgers.get(token)
        if ledger is None:
            raise RuntimeError(f"cell is not registered: {token}")
        return ledger

    def _ledger_path(self, token: str) -> Path:
        return self.ledger_dir / f"{token}.jsonl"  # type: ignore[attr-defined]

    def _legacy_ledger_path(self, token: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", token or "unknown")
        return self.ledger_dir / f"{safe}.jsonl"  # type: ignore[attr-defined]

    def _append_durable(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        if getattr(self, "verbose", False):
            print(line, end="", flush=True)

    def _append_legacy(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        if getattr(self, "verbose", False):
            print(line, end="", flush=True)

    @staticmethod
    def _last_record_equals(path: Path, expected: dict[str, Any]) -> bool:
        try:
            lines = path.read_bytes().splitlines()
            return bool(lines) and json.loads(lines[-1]) == expected
        except (OSError, json.JSONDecodeError):
            return False

    @staticmethod
    def _truncate_partial_record(path: Path) -> None:
        try:
            with path.open("r+b") as fh:
                data = fh.read()
                if not data or data.endswith(b"\n"):
                    return
                last_newline = data.rfind(b"\n")
                fh.truncate(last_newline + 1)
                fh.flush()
                os.fsync(fh.fileno())
        except FileNotFoundError:
            return

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("rb") as fh:
            os.fsync(fh.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def make_server(listen_host: str, port: int, ledger_dir: str | os.PathLike[str],
                chat_upstreams: dict[str, str] | None = None,
                anthropic_upstreams: dict[str, str] | None = None,
                openai_upstream: str = "https://api.openai.com",
                subbridge_upstream: str = "http://127.0.0.1:8317",
                cursor_upstream: str = DEFAULT_CURSOR_UPSTREAM,
                timeout_s: float = 300.0, capture_limit: int = 8 * 1024 * 1024,
                max_request_bytes: int = 64 * 1024 * 1024,
                verbose: bool = False,
                require_registered_tokens: bool = False) -> CountingProxyServer:
    chat = dict(DEFAULT_CHAT_UPSTREAMS)
    if chat_upstreams:
        chat.update({k: v for k, v in chat_upstreams.items() if v})
    anthropic = dict(DEFAULT_ANTHROPIC_UPSTREAMS)
    if anthropic_upstreams:
        anthropic.update({k: v for k, v in anthropic_upstreams.items() if v})
    httpd = CountingProxyServer((listen_host, port), CountingProxyHandler)
    httpd.upstreams = _urlsplit_map({
        "codex": "https://chatgpt.com",
        "openai": openai_upstream,
        "subbridge": subbridge_upstream,
        "cursor": cursor_upstream,
        "bridge": "http://127.0.0.1:" + os.environ.get("BENCH_BRIDGE_PORT", "4141"),
    })
    httpd.chat_upstreams = _urlsplit_map(chat)
    httpd.anthropic_upstreams = _urlsplit_map(anthropic)
    httpd.ledger_dir = Path(ledger_dir)
    httpd.timeout_s = timeout_s
    httpd.capture_limit = max(capture_limit, 4096)
    httpd.max_request_bytes = max(max_request_bytes, 0)
    httpd._ledger_condition = threading.Condition()
    httpd._cell_ledgers = {}
    httpd._legacy_in_flight = {}
    # Docker requires a non-loopback bind. In that mode the runner enables this
    # gate so arbitrary LAN clients cannot spend through a subscription route.
    httpd.require_registered_tokens = require_registered_tokens
    # Per-process salt prevents opaque provider IDs from being correlated across
    # proxy runs while preserving links inside one experiment ledger directory.
    httpd.identifier_salt = secrets.token_bytes(32)
    httpd.verbose = verbose
    return httpd


def start_in_thread(*args: Any, **kwargs: Any) -> tuple[CountingProxyServer, threading.Thread]:
    httpd = make_server(*args, **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, name="openbench-counting-proxy", daemon=True)
    thread.start()
    return httpd, thread


def _parse_upstream_args(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"bad upstream {item!r}; expected name=url")
        name, url = item.split("=", 1)
        out[name.strip()] = url.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenBench transparent counting proxy")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ledger-dir", required=True)
    parser.add_argument("--chat-upstream", action="append", default=[], help="name=url")
    parser.add_argument("--openai-upstream", default="https://api.openai.com")
    parser.add_argument("--subbridge-upstream", default="http://127.0.0.1:8317")
    parser.add_argument("--cursor-upstream", default=DEFAULT_CURSOR_UPSTREAM)
    parser.add_argument("--anthropic-upstream", action="append", default=[], help="name=url")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    httpd = make_server(
        args.listen_host, args.port, args.ledger_dir,
        chat_upstreams=_parse_upstream_args(args.chat_upstream),
        anthropic_upstreams=_parse_upstream_args(args.anthropic_upstream),
        openai_upstream=args.openai_upstream,
        subbridge_upstream=args.subbridge_upstream,
        cursor_upstream=args.cursor_upstream,
        timeout_s=args.timeout, verbose=args.verbose,
    )
    host, port = httpd.server_address[:2]
    print(f"listening=http://{host}:{port} ledger_dir={args.ledger_dir}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
