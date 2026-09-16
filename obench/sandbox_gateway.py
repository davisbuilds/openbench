"""Trial-scoped inference broker and loopback-to-Unix relay (stdlib only).

The broker owns OAuth and a fixed upstream. The solver gets neither credentials
nor a general network proxy. Transport injection is a Python testing seam only;
CLI configuration cannot select a destination. stdout contains metadata only.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import http.client
import http.server
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import socketserver
import ssl
import stat
import threading
import time
from typing import Callable

_UPSTREAM_HOST = "chatgpt.com"
_UPSTREAM_PATH = "/backend-api/codex/responses"
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


@dataclass(frozen=True)
class GatewayConfig:
    model: str
    effort: str
    max_requests: int
    max_body_bytes: int
    timeout_seconds: float
    max_concurrent_requests: int = 1

    def __post_init__(self):
        if not isinstance(self.model, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", self.model
        ):
            raise ValueError("invalid pinned model")
        if self.effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("invalid pinned effort")
        for name, ceiling in (
            ("max_requests", 10000),
            ("max_body_bytes", 16 * 1024 * 1024),
            ("max_concurrent_requests", 16),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid gateway limit")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (float, int))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 3600
        ):
            raise ValueError("invalid timeout")

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ValueError("invalid broker configuration")
        try:
            return cls(**value)
        except (TypeError, ValueError):
            raise ValueError("invalid broker configuration") from None

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class AuthCredentials:
    access_token: str = field(repr=False)
    account_id: str = field(repr=False)

    def __post_init__(self):
        for value in (self.access_token, self.account_id):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 32768
                or any(ord(c) < 33 or ord(c) > 126 for c in value)
            ):
                raise ValueError("invalid broker authentication")


def _json(value: bytes | str):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def invalid(_):
        raise ValueError("nonfinite JSON number")

    result = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)

    def check(item, depth=0):
        if depth > 64:
            raise ValueError("JSON nesting limit")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("nonfinite JSON number")
        if isinstance(item, dict):
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)

    check(result)
    return result


def _read_private_json(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024:
            raise ValueError()
        with os.fdopen(fd, "rb") as source:
            fd = None
            data = source.read(128 * 1024 + 1)
        if len(data) > 128 * 1024:
            raise ValueError()
        return _json(data)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ValueError("cannot load trusted gateway input") from None
    finally:
        if fd is not None:
            os.close(fd)


def load_auth(path) -> AuthCredentials:
    try:
        tokens = _read_private_json(path)["tokens"]
        return AuthCredentials(tokens["access_token"], tokens["account_id"])
    except (TypeError, KeyError, ValueError):
        raise ValueError("invalid broker authentication") from None


def _keys(value, allowed, required=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(allowed)
        or not set(required) <= set(value)
    ):
        raise ValueError("unsupported request shape")


def _string(value):
    if not isinstance(value, str):
        raise ValueError("expected text")


def _schema(value, depth=0):
    # Local JSON schema describes solver-side tool arguments; external references
    # are never resolved or sent as a hosted tool capability.
    if depth > 40:
        raise ValueError("schema too deep")
    if type(value) is bool:
        return
    _keys(
        value,
        {
            "type",
            "properties",
            "required",
            "additionalProperties",
            "$defs",
            "definitions",
            "$ref",
            "description",
            "title",
            "enum",
            "const",
            "default",
            "items",
            "prefixItems",
            "anyOf",
            "oneOf",
            "allOf",
            "not",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "pattern",
            "format",
            "nullable",
            "uniqueItems",
            "minProperties",
            "maxProperties",
            "encrypted",
        },
    )
    if "encrypted" in value and type(value["encrypted"]) is not bool:
        raise ValueError("invalid encrypted schema marker")
    if "$ref" in value and (
        not isinstance(value["$ref"], str) or not value["$ref"].startswith("#/")
    ):
        raise ValueError("external schema reference")
    if "type" in value:
        types = value["type"] if isinstance(value["type"], list) else [value["type"]]
        if not types or any(
            t
            not in {"object", "array", "string", "number", "integer", "boolean", "null"}
            for t in types
        ):
            raise ValueError("invalid schema type")
    for key in ("properties", "$defs", "definitions"):
        if key in value:
            if not isinstance(value[key], dict):
                raise ValueError("invalid schema map")
            for child in value[key].values():
                _schema(child, depth + 1)
    if "required" in value and (
        not isinstance(value["required"], list)
        or any(not isinstance(x, str) for x in value["required"])
    ):
        raise ValueError("invalid required names")
    for key in ("items", "additionalProperties", "not"):
        if key in value:
            _schema(value[key], depth + 1)
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if key in value:
            if not isinstance(value[key], list):
                raise ValueError("invalid schema alternatives")
            for child in value[key]:
                _schema(child, depth + 1)


def _text_content(value):
    if isinstance(value, str):
        return
    if not isinstance(value, list):
        raise ValueError("unsupported content")
    for item in value:
        _keys(item, {"type", "text"}, {"type", "text"})
        if item["type"] not in {"input_text", "output_text"}:
            raise ValueError("only text content is permitted")
        _string(item["text"])


def _validate_tools(tools, names, depth=0, prefix=""):
    if not isinstance(tools, list) or len(tools) > 128:
        raise ValueError("invalid tools")
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            _keys(
                tool,
                {"type", "name", "description", "tools"},
                {"type", "name", "tools"},
            )
            if (
                depth >= 2
                or not isinstance(tool["name"], str)
                or not _NAME.fullmatch(tool["name"])
            ):
                raise ValueError("invalid local namespace")
            if "description" in tool:
                _string(tool["description"])
            _validate_tools(
                tool["tools"], names, depth + 1, prefix + tool["name"] + "."
            )
            continue
        _keys(
            tool,
            {"type", "name", "description", "parameters", "strict", "format"},
            {"type", "name"},
        )
        name = tool["name"]
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or prefix + name in names
        ):
            raise ValueError("invalid local tool identity")
        names.add(prefix + name)
        if "description" in tool:
            _string(tool["description"])
        if tool["type"] == "function":
            if (
                "format" in tool
                or "parameters" not in tool
                or ("strict" in tool and type(tool["strict"]) is not bool)
            ):
                raise ValueError("invalid function tool")
            _schema(tool["parameters"])
        elif tool["type"] == "custom":
            if "parameters" in tool or "strict" in tool:
                raise ValueError("invalid custom tool")
            if "format" in tool:
                fmt = tool["format"]
                _keys(fmt, {"type", "syntax", "definition"}, {"type"})
                if fmt["type"] == "text":
                    if set(fmt) != {"type"}:
                        raise ValueError("invalid text tool")
                elif fmt["type"] == "grammar":
                    if set(fmt) != {"type", "syntax", "definition"} or fmt[
                        "syntax"
                    ] not in {"lark", "regex"}:
                        raise ValueError("invalid grammar tool")
                    _string(fmt["definition"])
                else:
                    raise ValueError("unsupported custom format")
        else:
            raise ValueError("hosted tools are forbidden")


def validate_body(raw: bytes, config: GatewayConfig) -> dict:
    """Admit local-tool Responses requests, never hosted retrieval capabilities."""
    try:
        body = _json(raw)
        _keys(
            body,
            {
                "model",
                "reasoning",
                "stream",
                "store",
                "input",
                "instructions",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
                "include",
                "text",
                "service_tier",
                "prompt_cache_key",
                "max_output_tokens",
                "client_metadata",
            },
            {"model", "reasoning", "stream", "store", "input"},
        )
        if (
            body["model"] != config.model
            or body["stream"] is not True
            or body["store"] is not False
        ):
            raise ValueError("pinned request mismatch")
        _keys(body["reasoning"], {"effort", "summary", "context"}, {"effort"})
        if body["reasoning"]["effort"] != config.effort:
            raise ValueError("pinned effort mismatch")
        if body["reasoning"].get("summary", "auto") not in {
            "auto",
            "concise",
            "detailed",
            "none",
        }:
            raise ValueError("invalid reasoning summary")
        if body["reasoning"].get("context", "all_turns") != "all_turns":
            raise ValueError("unsupported reasoning context")
        if "client_metadata" in body:
            metadata = body["client_metadata"]
            _keys(
                metadata,
                {
                    "x-codex-window-id",
                    "x-codex-installation-id",
                    "root_turn_id",
                    "turn_id",
                    "x-codex-turn-metadata",
                    "session_id",
                    "thread_id",
                },
            )
            if any(not isinstance(v, str) or len(v) > 16384 for v in metadata.values()):
                raise ValueError("invalid client metadata")
        for key in ("instructions", "prompt_cache_key"):
            if key in body:
                _string(body[key])
        if "service_tier" in body and body["service_tier"] != "default":
            raise ValueError("unsupported service tier")
        if (
            "parallel_tool_calls" in body
            and type(body["parallel_tool_calls"]) is not bool
        ):
            raise ValueError("invalid tool concurrency")
        if "include" in body and body["include"] != ["reasoning.encrypted_content"]:
            raise ValueError("unsupported include")
        if "max_output_tokens" in body and (
            type(body["max_output_tokens"]) is not int
            or not 1 <= body["max_output_tokens"] <= 1000000
        ):
            raise ValueError("invalid output limit")
        if "text" in body:
            _keys(body["text"], {"verbosity", "format"})
            if body["text"].get("verbosity", "medium") not in {"low", "medium", "high"}:
                raise ValueError("invalid verbosity")
            if "format" in body["text"]:
                _keys(body["text"]["format"], {"type"}, {"type"})
                if body["text"]["format"]["type"] != "text":
                    raise ValueError("unsupported output format")
        names = set()
        _validate_tools(body.get("tools", []), names)
        choice = body.get("tool_choice", "auto")
        if isinstance(choice, str):
            if choice not in {"auto", "none", "required"}:
                raise ValueError("invalid tool choice")
        else:
            _keys(choice, {"type", "name"}, {"type", "name"})
            if (
                choice["type"] not in {"function", "custom"}
                or choice["name"] not in names
            ):
                raise ValueError("unknown local tool")
        if not isinstance(body["input"], list):
            raise ValueError("input must be explicit local items")
        for item in body["input"]:
            if not isinstance(item, dict):
                raise ValueError("invalid input item")
            kind = item.get("type", "message")
            if kind == "additional_tools":
                _keys(item, {"type", "id", "role", "tools"}, {"type", "role", "tools"})
                if item["role"] != "developer":
                    raise ValueError("invalid additional tools role")
                _validate_tools(item["tools"], names)
            elif kind == "message":
                _keys(
                    item,
                    {"type", "role", "content", "id", "status", "phase"},
                    {"role", "content"},
                )
                if item["role"] not in {"system", "developer", "user", "assistant"}:
                    raise ValueError("unsupported role")
                if "phase" in item and item["phase"] not in {
                    "commentary",
                    "final_answer",
                }:
                    raise ValueError("unsupported message phase")
                _text_content(item["content"])
            elif kind in {"function_call", "custom_tool_call"}:
                field_name = "arguments" if kind == "function_call" else "input"
                _keys(
                    item,
                    {"type", "id", "call_id", "name", "namespace", field_name, "status"},
                    {"type", "call_id", "name", field_name},
                )
                for key in ("call_id", "name", field_name):
                    _string(item[key])
                name = item["name"]
                if "namespace" in item:
                    namespace = item["namespace"]
                    if not isinstance(namespace, str) or not all(
                        _NAME.fullmatch(part) for part in namespace.split(".")
                    ):
                        raise ValueError("invalid local tool namespace")
                    name = namespace + "." + name
                # The pinned provider may omit namespace from a returned call.
                # Admit its short spelling only when one declared local tool
                # matches; do not infer an explicit or ambiguous namespace.
                unqualified_match = (
                    "namespace" not in item and "." not in name
                    and sum(candidate.rsplit(".", 1)[-1] == name for candidate in names) == 1
                )
                if name not in names and not unqualified_match:
                    raise ValueError("unknown local tool history")
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                _keys(
                    item,
                    {"type", "id", "call_id", "output", "status"},
                    {"type", "call_id", "output"},
                )
                _string(item["call_id"])
                _text_content(item["output"])
            elif kind == "reasoning":
                _keys(
                    item,
                    {"type", "id", "summary", "encrypted_content", "status"},
                    {"type", "summary"},
                )
                if not isinstance(item["summary"], list):
                    raise ValueError("invalid reasoning summary")
                for part in item["summary"]:
                    _keys(part, {"type", "text"}, {"type", "text"})
                    if part["type"] != "summary_text":
                        raise ValueError("unsupported summary")
                    _string(part["text"])
                if "encrypted_content" in item:
                    _string(item["encrypted_content"])
            else:
                raise ValueError("hosted input and references are forbidden")
            for key in ("id", "status"):
                if key in item:
                    _string(item[key])
        return body
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError("request violates inference policy") from None


def _socket_peer(sock):
    if sock is None:
        return None
    try:
        peer = sock.getpeername()
        if not isinstance(peer, tuple) or len(peer) < 2:
            return None
        address, port = peer[:2]
        if not isinstance(address, str) or type(port) is not int:
            return None
        ipaddress.ip_address(address)
        return {"ip": address, "port": port}
    except (OSError, ValueError):
        return None


class UpstreamStream:
    """Own both HTTP response and its connection; close interrupts blocking IO."""

    def __init__(self, connection, response):
        self.connection, self.response = connection, response
        self.status = response.status
        self.headers = response.headers
        self._close_lock = threading.Lock()
        self._closed = False
        sock = connection.sock
        if sock is None:
            try:
                sock = response.fp.raw._sock
            except AttributeError:
                pass
        self.peer = _socket_peer(sock)

    def read(self, size):
        return self.response.read1(size)

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        # HTTP/1.0 responses may have detached the connection socket; the
        # response still owns it. Shutdown first so cancellation wakes a reader.
        sock = self.connection.sock
        if sock is None:
            try:
                sock = self.response.fp.raw._sock
            except AttributeError:
                pass
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.connection.close()
        self.response.close()


class _CancellableHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancelled = threading.Event()
        self.observed_peer = None

    def cancel(self):
        self.cancelled.set()
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()

    def connect(self):
        if self.cancelled.is_set():
            raise OSError("request cancelled")
        super().connect()
        self.observed_peer = _socket_peer(self.sock)
        if self.cancelled.is_set():
            self.cancel()
            raise OSError("request cancelled")

    def send(self, data):
        if self.cancelled.is_set():
            raise OSError("request cancelled")
        super().send(data)


class _PendingConnection:
    def __init__(self, connection):
        self.connection = connection

    def close(self):
        self.connection.cancel()


def _production_transport(body: bytes, headers: dict, timeout: float, *, register=None):
    conn = _CancellableHTTPSConnection(
        _UPSTREAM_HOST, timeout=timeout, context=ssl.create_default_context()
    )
    try:
        if register is not None:
            register(_PendingConnection(conn))
        conn.request("POST", _UPSTREAM_PATH, body=body, headers=headers)
        return UpstreamStream(conn, conn.getresponse())
    except BaseException:
        conn.close()
        raise


def _emit(record):
    print(json.dumps(record, separators=(",", ":")), flush=True)


class _BoundedServer(socketserver.ThreadingMixIn):
    daemon_threads = True
    block_on_close = False

    def _init_limits(self, max_connections=8):
        self._connections = set()
        self._upstreams = set()
        self._lock = threading.Lock()
        self._receipt_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_connections)
        self._revoked = threading.Event()
        self._stopped = False
        self._receipt_failed = False
        self._drain_incomplete = False
        self._stop_complete = threading.Event()

    def process_request(self, request, client_address):
        if self._revoked.is_set() or not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self._lock:
            self._connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._lock:
                self._connections.discard(request)
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._lock:
                self._connections.discard(request)
            self._slots.release()

    def record(self, record):
        sink = getattr(self, "receipt", None)
        if sink is None:
            return
        try:
            with self._receipt_lock:
                sink(record)
        except Exception:
            # Continuing without an audit sink would silently lose evidence.
            self._receipt_failed = True
            self._revoked.set()
            threading.Thread(target=self.stop, daemon=True).start()

    def handle_error(self, *_):
        # socketserver's default traceback can expose request/credential context.
        pass

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._revoked.set()
            sockets = list(self._connections)
            upstreams = list(self._upstreams)
        try:
            for conn in sockets:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
            for upstream in upstreams:
                upstream.close()
            self.shutdown()
            deadline = time.monotonic() + min(self.timeout_seconds, 2.0)
            while time.monotonic() < deadline:
                with self._lock:
                    if not self._connections:
                        break
                time.sleep(0.01)
            with self._lock:
                self._drain_incomplete = bool(self._connections)
            self.server_close()
        except Exception:
            self._drain_incomplete = True
            self.server_close()
        finally:
            self._stop_complete.set()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenBenchGateway"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.timeout_seconds)
        self._deadline_at = time.monotonic() + self.server.timeout_seconds
        self._deadline_expired = threading.Event()

        def expire_connection():
            self._deadline_expired.set()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        self._deadline_timer = threading.Timer(
            self.server.timeout_seconds, expire_connection
        )
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def finish(self):
        self._deadline_timer.cancel()
        super().finish()

    def log_message(self, *_):
        pass

    def send_error(self, code, message=None, explain=None):
        self._error(code, "invalid_http")

    def handle_expect_100(self):
        self._error(400, "unsupported_framing")
        return False

    def _error(self, status, code):
        self.close_connection = True
        if getattr(self, "_stream_started", False):
            return
        self.server.record({"event": "rejected", "status": status, "code": code})
        payload = json.dumps({"error": {"code": code}}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass

    def do_POST(self):
        self.close_connection = True
        if self.path != "/responses":
            self._error(404, "unsupported_route")
            return
        names = [key.lower() for key in self.headers.keys()]
        if (
            len(names) != len(set(names))
            or sum(len(k) + len(v) for k, v in self.headers.items()) > 32768
            or any(
                not _HEADER.fullmatch(k) or any(ord(c) < 32 or ord(c) == 127 for c in v)
                for k, v in self.headers.items()
            )
            or any(
                key in names
                for key in (
                    "transfer-encoding",
                    "content-encoding",
                    "upgrade",
                    "expect",
                    "trailer",
                )
            )
        ):
            self._error(400, "unsupported_framing")
            return
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,10}", length):
            self._error(411, "content_length_required")
            return
        size = int(length)
        if size > self.server.max_body_bytes:
            self._error(413, "body_too_large")
            return
        if self.headers.get("Content-Type", "").lower() not in {
            "application/json",
            "application/json; charset=utf-8",
        }:
            self._error(400, "json_required")
            return
        try:
            body = self.rfile.read(size)
            if len(body) != size:
                self._error(400, "incomplete_body")
                return
            self.server.forward(self, body)
        except (OSError, ValueError):
            self._error(400, "invalid_request")

    def do_CONNECT(self):
        self._error(405, "method_not_allowed")

    do_GET = do_CONNECT
    do_PUT = do_CONNECT
    do_DELETE = do_CONNECT
    do_PATCH = do_CONNECT
    do_HEAD = do_CONNECT
    do_OPTIONS = do_CONNECT


def _watch_disconnect(connection, stop, cancel):
    while not stop.wait(0.05):
        try:
            readable, _, _ = select.select([connection], [], [], 0)
            if readable:
                # The complete one-shot request body is already consumed. EOF
                # or further/pipelined bytes both revoke this connection.
                connection.recv(1, socket.MSG_PEEK)
                cancel()
                return
        except (OSError, ValueError):
            cancel()
            return


def _headerless_responses_prefix(upstream):
    """The fixed OAuth endpoint can omit Content-Type; verify SSE before relay."""
    prefix = b""
    limit = 1024 * 1024
    while len(prefix) < limit:
        chunk = upstream.read(min(16384, limit - len(prefix)))
        if not chunk:
            break
        prefix += chunk
        normalized = prefix.replace(b"\r\n", b"\n")
        if b"\n\n" not in normalized:
            continue
        first = normalized.split(b"\n\n", 1)[0]
        lines = first.split(b"\n")
        data = [line[5:].lstrip() for line in lines if line.startswith(b"data:")]
        events = [line[6:].strip() for line in lines if line.startswith(b"event:")]
        event = _json(b"\n".join(data))
        if (not isinstance(event, dict) or event.get("type") != "response.created"
                or events not in ([], [b"response.created"])
                or not isinstance(event.get("response"), dict)
                or event["response"].get("object") != "response"
                or not isinstance(event["response"].get("id"), str)
                or not event["response"]["id"]):
            raise ValueError("invalid Responses stream prefix")
        return prefix
    raise ValueError("missing or oversized Responses stream prefix")


def _stream(handler, server, upstream):
    sent = 0
    usage = {}
    event_buffer = b""
    completed = False
    content_type = upstream.headers.get("Content-Type")
    if (upstream.status != 200 or (content_type is not None and
            content_type.split(";", 1)[0].strip().lower() != "text/event-stream")):
        handler._error(502, "upstream_rejected")
        return "upstream_rejected", sent, usage
    prefix = b""
    if content_type is None:
        try:
            prefix = _headerless_responses_prefix(upstream)
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            handler._error(502, "upstream_rejected")
            return "upstream_rejected", sent, usage
    handler._stream_started = True
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        while not server._revoked.is_set():
            chunk = prefix or upstream.read(16384)
            prefix = b""
            if not chunk:
                return ("complete" if completed else "upstream_incomplete"), sent, usage
            sent += len(chunk)
            if sent > _MAX_RESPONSE_BYTES:
                return "response_limit", sent, usage
            handler.wfile.write(chunk)
            handler.wfile.flush()
            # Parse metadata only. Never persist events, prompts or model text.
            event_buffer += chunk
            if len(event_buffer) > 1024 * 1024:
                event_buffer = b""
            while b"\n" in event_buffer:
                line, event_buffer = event_buffer.split(b"\n", 1)
                if not line.startswith(b"data: "):
                    continue
                try:
                    if line[6:].strip() == b"[DONE]":
                        completed = True
                        continue
                    event = _json(line[6:])
                    if event.get("type") == "response.completed":
                        completed = True
                    candidate = event.get("response", {}).get("usage", {})
                    if isinstance(candidate, dict):
                        for key in ("input_tokens", "output_tokens", "total_tokens"):
                            value = candidate.get(key)
                            if type(value) is int and value >= 0:
                                usage[key] = value
                except (
                    ValueError,
                    TypeError,
                    AttributeError,
                    UnicodeError,
                    RecursionError,
                ):
                    pass
        return "revoked", sent, usage
    except TimeoutError:
        return "timeout", sent, usage
    except (OSError, ValueError, http.client.HTTPException):
        return "stream_interrupted", sent, usage


class BrokerServer(_BoundedServer, socketserver.UnixStreamServer):
    def __init__(
        self,
        socket_path,
        config: GatewayConfig,
        auth: AuthCredentials,
        *,
        transport: Callable | None = None,
        receipt: Callable | None = None,
    ):
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError("broker socket must be absolute")
        self.config, self._auth = config, auth
        self.transport = transport or _production_transport
        self._production = transport is None
        self.receipt = receipt or _emit
        self.max_body_bytes, self.timeout_seconds = (
            config.max_body_bytes,
            config.timeout_seconds,
        )
        self._init_limits(max(4, config.max_concurrent_requests * 2))
        self._request_count = self._active_requests = 0
        # Never unlink a pre-existing socket: it may belong to another trial.
        super().__init__(str(self.socket_path), _Handler)
        self._socket_identity = self.socket_path.stat().st_ino
        try:
            os.chmod(self.socket_path, 0o660)
        except OSError:
            self.server_close()
            raise

    def forward(self, handler, raw):
        try:
            validate_body(raw, self.config)
        except ValueError:
            handler._error(400, "inference_policy")
            return
        with self._lock:
            if self._revoked.is_set():
                handler._error(503, "revoked")
                return
            if (
                self._request_count >= self.config.max_requests
                or self._active_requests >= self.config.max_concurrent_requests
            ):
                handler._error(429, "request_limit")
                return
            self._request_count += 1
            request_id = self._request_count
            self._active_requests += 1
        upstream = None
        pending = None
        timed_out = threading.Event()

        def expire():
            timed_out.set()
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if upstream is not None:
                upstream.close()
            if pending is not None:
                pending.close()

        disconnected = threading.Event()
        watching = threading.Event()

        def cancel_client():
            disconnected.set()
            if upstream is not None:
                upstream.close()
            if pending is not None:
                pending.close()

        watcher = threading.Thread(
            target=_watch_disconnect,
            args=(handler.connection, watching, cancel_client),
            daemon=True,
        )
        watcher.start()
        timer = threading.Timer(self.timeout_seconds, expire)
        timer.daemon = True
        timer.start()
        outcome, sent, usage = "upstream_unavailable", 0, {}
        try:
            headers = {
                "Authorization": "Bearer " + self._auth.access_token,
                "ChatGPT-Account-Id": self._auth.account_id,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Content-Length": str(len(raw)),
                "Connection": "close",
                "x-openai-internal-codex-responses-lite": "true",
            }
            if self._production:

                def register(connection):
                    nonlocal pending
                    pending = connection
                    with self._lock:
                        self._upstreams.add(connection)
                    if (
                        disconnected.is_set()
                        or timed_out.is_set()
                        or self._revoked.is_set()
                    ):
                        connection.close()
                        raise OSError("request cancelled")

                upstream = self.transport(
                    raw, headers, self.timeout_seconds, register=register
                )
            else:
                upstream = self.transport(raw, headers, self.timeout_seconds)
            with self._lock:
                self._upstreams.add(upstream)
            if timed_out.is_set() or self._revoked.is_set() or disconnected.is_set():
                outcome = "timeout" if timed_out.is_set() else "revoked"
            else:
                outcome, sent, usage = _stream(handler, self, upstream)
        except TimeoutError:
            outcome = "timeout"
            handler._error(502, "upstream_timeout")
        except (OSError, ValueError, http.client.HTTPException):
            handler._error(502, "upstream_unavailable")
        finally:
            timer.cancel()
            watching.set()
            if pending is not None:
                with self._lock:
                    self._upstreams.discard(pending)
                pending.close()
            if upstream is not None:
                with self._lock:
                    self._upstreams.discard(upstream)
                upstream.close()
            with self._lock:
                self._active_requests -= 1
            if (
                timed_out.is_set()
                or handler._deadline_expired.is_set()
                or time.monotonic() >= handler._deadline_at
            ):
                outcome = "timeout"
            elif self._revoked.is_set():
                outcome = "revoked"
            elif disconnected.is_set():
                outcome = "client_disconnected"
            self.record(
                {
                    "event": "request",
                    "request_id": request_id,
                    "model": self.config.model,
                    "effort": self.config.effort,
                    "input_bytes": len(raw),
                    "response_bytes": sent,
                    "outcome": outcome,
                    "upstream_status": getattr(upstream, "status", None),
                    "usage": usage,
                    "upstream_peer": (
                        getattr(upstream, "peer", None)
                        if upstream is not None
                        else getattr(
                            getattr(pending, "connection", None), "observed_peer", None
                        )
                    ),
                }
            )

    def server_close(self):
        super().server_close()
        try:
            if self.socket_path.lstat().st_ino == self._socket_identity:
                self.socket_path.unlink()
        except (FileNotFoundError, AttributeError):
            pass


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class RelayServer(_BoundedServer, http.server.HTTPServer):
    def __init__(
        self,
        address,
        socket_path,
        *,
        max_body_bytes=4 * 1024 * 1024,
        timeout_seconds=300,
    ):
        if address[0] != "127.0.0.1":
            raise ValueError("relay must bind IPv4 loopback")
        self.socket_path = Path(socket_path)
        if not self.socket_path.is_absolute():
            raise ValueError("relay socket must be absolute")
        # Reuse the same bounded limit validation as broker CLI.
        GatewayConfig("relay", "low", 1, max_body_bytes, timeout_seconds)
        self.max_body_bytes, self.timeout_seconds = max_body_bytes, timeout_seconds
        self._init_limits()
        super().__init__(address, _Handler)

    def forward(self, handler, raw):
        conn = _UnixHTTPConnection(self.socket_path, self.timeout_seconds)
        upstream = None
        watching = threading.Event()

        def cancel_client():
            if upstream is not None:
                upstream.close()
            else:
                conn.close()

        watcher = threading.Thread(
            target=_watch_disconnect,
            args=(handler.connection, watching, cancel_client),
            daemon=True,
        )
        watcher.start()
        try:
            conn.request(
                "POST",
                "/responses",
                raw,
                {"Content-Type": "application/json", "Connection": "close"},
            )
            upstream = UpstreamStream(conn, conn.getresponse())
            with self._lock:
                self._upstreams.add(upstream)
            if upstream.status != 200:
                handler._error(
                    upstream.status
                    if upstream.status in {400, 404, 411, 413, 429, 503}
                    else 502,
                    "broker_rejected",
                )
            else:
                _stream(handler, self, upstream)
        except (OSError, ValueError, http.client.HTTPException):
            handler._error(502, "broker_unavailable")
        finally:
            watching.set()
            if upstream is not None:
                with self._lock:
                    self._upstreams.discard(upstream)
                upstream.close()
            conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="role", required=True)
    broker = sub.add_parser("broker")
    broker.add_argument("--socket", required=True)
    broker.add_argument("--config", required=True)
    broker.add_argument("--auth", required=True)
    relay = sub.add_parser("relay")
    relay.add_argument("--socket", required=True)
    relay.add_argument("--port", type=int, default=8765)
    relay.add_argument("--max-body-bytes", type=int, default=4 * 1024 * 1024)
    relay.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    try:
        if args.role == "broker":
            server = BrokerServer(
                args.socket,
                GatewayConfig.from_dict(_read_private_json(args.config)),
                load_auth(args.auth),
            )
        else:
            server = RelayServer(
                ("127.0.0.1", args.port),
                args.socket,
                max_body_bytes=args.max_body_bytes,
                timeout_seconds=args.timeout_seconds,
            )
    except (ValueError, OSError):
        _emit({"event": "startup_failed", "role": args.role})
        return 1
    stopping = threading.Event()

    def stop(_signal, _frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.stop, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    ready = {"event": "ready", "role": args.role}
    if args.role == "relay":
        ready["port"] = server.server_address[1]
    _emit(ready)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        if server._stopped and not server._stop_complete.wait(3):
            server._drain_incomplete = True
        server.server_close()
    clean = not (server._receipt_failed or server._drain_incomplete)
    _emit({"event": "stopped", "role": args.role, "clean": clean})
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
