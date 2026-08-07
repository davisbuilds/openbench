"""Transparent MCP stdio relay with a privacy-safe, sealed call ledger.

The collector observes newline-delimited JSON-RPC while relaying the original
bytes unchanged. It is intentionally not a JSON-RPC endpoint: messages outside
``tools/call`` request/response pairs are never interpreted or rewritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence


EMPTY_ROOT_HASH = hashlib.sha256(b"").hexdigest()
MAX_ARGUMENT_NODES = 10_000
MAX_ARGUMENT_DEPTH = 64
META_KEYS = {
    "error": "computer-use-mcp/error",
    "outcome": "computer-use-mcp/outcome",
    "focus": "computer-use-mcp/focus",
    "delivery": "computer-use-mcp/delivery",
}
ERROR_CODES = frozenset({
    "STALE_ELEMENT",
    "AMBIGUOUS_TARGET",
    "CONFIRMATION_REQUIRED",
    "POLICY_DENIED",
    "SCREEN_LOCKED",
    "USER_INTERFERENCE",
    "APP_NOT_FOUND",
    "ELEMENT_NOT_FOUND",
    "NOT_SETTABLE",
    "OFFSCREEN_TARGET",
    "APP_LEASE_HELD",
    "DAEMON_UNAUTHORIZED",
})
OUTCOME_CLASSIFICATIONS = frozenset({
    "success",
    "unsupported",
    "effect_not_verified",
    "verifier_ambiguous",
})
FAILURE_DOMAINS = frozenset({
    "targeting",
    "unsupported",
    "coercion",
    "transport",
    "verification",
    "web",
    "app_specific_semantics",
})
DELIVERY_TIERS = frozenset({
    "tier1-ax-action",
    "tier1-ax-attribute",
    "tier2-per-window-nsevent",
    "tier25-skylight-sleventpostto-pid",
    "tier3-cgeventpostto-pid",
    "tier4-global-cursor",
    "tier4-global-session-tap",
    "pasteboard",
    "launchservices",
    "ax-window-management",
})
FALLBACK_REASONS = frozenset({
    "ax-action-unsupported",
    "window-number-unresolved",
    "window-frame-unresolved",
    "event-bridge-failed",
    "skylight-unavailable",
    "global-cursor-requested",
    "no-scroll-container-found",
    "scroll-action-unverified",
    "chain-ax-press-unverified",
    "chain-ax-confirm-unverified",
    "chain-ax-open-unverified",
    "chain-ax-pick-unverified",
    "chain-selection-relay-unverified",
    "chain-child-action-unverified",
    "chain-ancestor-action-unverified",
})
CHAIN_RUNGS = frozenset({
    "ax-press",
    "ax-confirm",
    "ax-open",
    "ax-pick",
    "selection-relay",
    "child-press",
    "ancestor-press",
})
COMPUTER_USE_TOOLS = frozenset({
    "batch",
    "click",
    "click_menu_item",
    "delete_skill",
    "drag",
    "find",
    "get_app_state",
    "get_skill",
    "health_report",
    "list_apps",
    "list_skills",
    "list_windows",
    "manage_window",
    "open_app",
    "open_url",
    "page",
    "perform_secondary_action",
    "press_key",
    "read_clipboard",
    "read_text",
    "record_skill_start",
    "record_skill_stop",
    "run_skill",
    "save_skill",
    "scroll",
    "select_text",
    "set_value",
    "type_text",
    "wait_for",
    "write_clipboard",
})
OUTCOME_VERIFICATION_FLAGS = frozenset({
    "target_relocated",
    "before_selected",
    "after_selected",
    "before_focused",
    "after_focused",
    "rendered_text_changed",
    "focused_element_changed",
    "window_title_changed",
    "window_frame_changed",
    "scroll_position_changed",
    "scroll_content_changed",
    "scroll_at_extent",
    "target_in_web_area",
    "independent_element_changed",
    "target_state_changed",
})


class CollectorError(RuntimeError):
    """Raised when collection cannot preserve or durably account for traffic."""


class LedgerIntegrityError(CollectorError):
    """Raised when a collector ledger is incomplete or fails its hash chain."""


class ArgumentDigestLimitError(CollectorError):
    """Raised when argument normalization exceeds its safe complexity budget."""


@dataclass(frozen=True)
class CollectionResult:
    returncode: int
    ledger_path: Path
    call_count: int
    root_hash: str
    integrity_ok: bool
    malformed_frames: int
    partial_frames: int
    missing_responses: int
    input_incomplete: bool


@dataclass(frozen=True)
class LedgerVerification:
    run_id: str
    trial_id: str
    call_count: int
    root_hash: str
    integrity_ok: bool
    summary: Mapping[str, Any]


@dataclass
class _PendingCall:
    request_id: Any
    tool: str
    argument_digest: str
    request_bytes: int
    request_unix_ns: int
    request_monotonic_ns: int


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _id_key(value: Any) -> str:
    return f"{type(value).__name__}:{_canonical_bytes(value).decode('utf-8')}"


def _length_bucket(length: int) -> str:
    if length == 0:
        return "0"
    for upper in (4, 16, 64, 256, 1024):
        if length <= upper:
            return f"1-{upper}"
    return "1025+"


def _feed_digest_token(hasher: Any, token: str) -> None:
    encoded = token.encode("utf-8", "surrogatepass")
    hasher.update(str(len(encoded)).encode("ascii"))
    hasher.update(b":")
    hasher.update(encoded)


def normalized_argument_digest(arguments: Any) -> str:
    """Incrementally digest argument shape under a strict complexity budget."""
    hasher = hashlib.sha256()
    remaining = [MAX_ARGUMENT_NODES]

    def feed(value: Any, depth: int) -> None:
        if depth > MAX_ARGUMENT_DEPTH or remaining[0] <= 0:
            raise ArgumentDigestLimitError(
                "MCP argument shape exceeds normalization budget"
            )
        remaining[0] -= 1
        if value is None:
            _feed_digest_token(hasher, "null")
        elif isinstance(value, bool):
            _feed_digest_token(hasher, "boolean")
        elif isinstance(value, int):
            _feed_digest_token(hasher, "integer")
        elif isinstance(value, float):
            _feed_digest_token(hasher, "number")
        elif isinstance(value, str):
            _feed_digest_token(hasher, f"string:{_length_bucket(len(value))}")
        elif isinstance(value, list):
            if len(value) > remaining[0]:
                raise ArgumentDigestLimitError(
                    "MCP argument shape exceeds normalization budget"
                )
            _feed_digest_token(hasher, f"array:{_length_bucket(len(value))}")
            for item in value:
                feed(item, depth + 1)
        elif isinstance(value, dict):
            if len(value) > remaining[0]:
                raise ArgumentDigestLimitError(
                    "MCP argument shape exceeds normalization budget"
                )
            _feed_digest_token(hasher, f"object:{_length_bucket(len(value))}")
            for key in sorted(value, key=str):
                _feed_digest_token(hasher, f"field:{key}")
                feed(value[key], depth + 1)
        else:
            _feed_digest_token(hasher, f"type:{type(value).__name__}")

    feed(arguments, 0)
    return "sha256:" + hasher.hexdigest()


def _known_enum(value: Any, allowed: frozenset[str]) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value in allowed else "<unrecognized>"


def _boolean_fields(value: Any, allowed: frozenset[str]) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in sorted(allowed)
        if isinstance(value.get(key), bool)
    }


def _privacy_safe_meta(metadata: Any) -> dict[str, Any]:
    """Retain only source-defined categorical and boolean MCP telemetry."""
    metadata = metadata if isinstance(metadata, dict) else {}

    raw_error = metadata.get(META_KEYS["error"])
    error = None
    if isinstance(raw_error, dict) and "code" in raw_error:
        error = {"code": _known_enum(raw_error.get("code"), ERROR_CODES)}

    raw_outcome = metadata.get(META_KEYS["outcome"])
    outcome = None
    if isinstance(raw_outcome, dict):
        outcome = {
            "classification": _known_enum(
                raw_outcome.get("classification"), OUTCOME_CLASSIFICATIONS
            ),
            "failure_domain": _known_enum(
                raw_outcome.get("failure_domain"), FAILURE_DOMAINS
            ),
            "web_ax_echo_risk": (
                raw_outcome.get("web_ax_echo_risk")
                if isinstance(raw_outcome.get("web_ax_echo_risk"), bool)
                else None
            ),
            "verification": _boolean_fields(
                raw_outcome.get("verification"), OUTCOME_VERIFICATION_FLAGS
            ),
        }

    raw_focus = metadata.get(META_KEYS["focus"])
    focus = _boolean_fields(
        raw_focus,
        frozenset({
            "focus_changed",
            "focus_change_allowed",
            "cursor_movement_allowed",
        }),
    )
    if not isinstance(raw_focus, dict):
        focus = None

    raw_delivery = metadata.get(META_KEYS["delivery"])
    delivery = None
    if isinstance(raw_delivery, dict):
        reasons = raw_delivery.get("fallback_reasons")
        delivery = {
            "delivery_tier": _known_enum(
                raw_delivery.get("delivery_tier"), DELIVERY_TIERS
            ),
            "fallback_reasons": (
                [
                    _known_enum(reason, FALLBACK_REASONS)
                    for reason in reasons
                    if isinstance(reason, str)
                ]
                if isinstance(reasons, list)
                else []
            ),
            "chain_rung": _known_enum(
                raw_delivery.get("chain_rung"), CHAIN_RUNGS
            ),
            "ui_changed": (
                raw_delivery.get("ui_changed")
                if isinstance(raw_delivery.get("ui_changed"), bool)
                else None
            ),
        }
    return {
        "error": error,
        "outcome": outcome,
        "focus": focus,
        "delivery": delivery,
    }


def _validated_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(summary)
    normalized.setdefault("input_incomplete", False)
    integer_fields = (
        "returncode",
        "malformed_frames",
        "partial_frames",
        "duplicate_request_ids",
        "missing_responses",
    )
    for field in integer_fields:
        value = normalized.get(field)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or (field != "returncode" and value < 0)
        ):
            raise ValueError(f"invalid MCP collector summary {field}")
    if not isinstance(normalized.get("input_incomplete"), bool):
        raise ValueError("invalid MCP collector summary input_incomplete")
    expected_integrity = (
        normalized["returncode"] == 0
        and normalized["malformed_frames"] == 0
        and normalized["partial_frames"] == 0
        and normalized["duplicate_request_ids"] == 0
        and normalized["missing_responses"] == 0
        and not normalized["input_incomplete"]
    )
    if normalized.get("integrity_ok") is not expected_integrity:
        raise ValueError("MCP collector summary integrity state disagrees")
    return normalized


class CallLedger:
    """Exclusive, append-only, hash-chained ledger for one benchmark trial."""

    def __init__(self, path: str | os.PathLike[str], run_id: str, trial_id: str):
        if not run_id or not trial_id:
            raise ValueError("run_id and trial_id must be non-empty")
        self.path = Path(path)
        self.run_id = run_id
        self.trial_id = trial_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
        self._fd = os.open(self.path, flags, 0o600)
        self._lock = threading.Lock()
        self._sealed = False
        self._call_count = 0
        self._root_hash = EMPTY_ROOT_HASH
        try:
            self._fsync_directory()
        except BaseException:
            os.close(self._fd)
            self._sealed = True
            raise

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def root_hash(self) -> str:
        return self._root_hash

    def append_call(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            if self._sealed:
                raise CollectorError("cannot append to sealed MCP collector ledger")
            sequence = self._call_count + 1
            chained = dict(record)
            for reserved in (
                "record_type",
                "run_id",
                "trial_id",
                "sequence",
                "previous_hash",
                "record_hash",
            ):
                chained.pop(reserved, None)
            chained.update(
                {
                    "record_type": "tool_call",
                    "run_id": self.run_id,
                    "trial_id": self.trial_id,
                    "sequence": sequence,
                    "previous_hash": self._root_hash,
                }
            )
            record_hash = hashlib.sha256(_canonical_bytes(chained)).hexdigest()
            chained["record_hash"] = record_hash
            self._append_durable(chained)
            self._call_count = sequence
            self._root_hash = record_hash

    def seal(self, summary: Mapping[str, Any]) -> CollectionResult:
        validated_summary = _validated_summary(summary)
        with self._lock:
            if self._sealed:
                raise CollectorError("MCP collector ledger is already sealed")
            terminal = {
                "record_type": "ledger_seal",
                "run_id": self.run_id,
                "trial_id": self.trial_id,
                "call_count": self._call_count,
                "last_sequence": self._call_count,
                "root_hash": self._root_hash,
                "summary": validated_summary,
            }
            terminal["seal_hash"] = hashlib.sha256(
                _canonical_bytes(terminal)
            ).hexdigest()
            self._append_durable(terminal)
            os.close(self._fd)
            self._sealed = True
            self._fsync_directory()
            return CollectionResult(
                returncode=validated_summary["returncode"],
                ledger_path=self.path,
                call_count=self._call_count,
                root_hash=self._root_hash,
                integrity_ok=validated_summary["integrity_ok"],
                malformed_frames=validated_summary["malformed_frames"],
                partial_frames=validated_summary["partial_frames"],
                missing_responses=validated_summary["missing_responses"],
                input_incomplete=validated_summary["input_incomplete"],
            )

    def abort(self) -> None:
        with self._lock:
            if not self._sealed:
                os.close(self._fd)
                self._sealed = True

    def _append_durable(self, record: Mapping[str, Any]) -> None:
        payload = _canonical_bytes(record) + b"\n"
        offset = 0
        while offset < len(payload):
            written = os.write(self._fd, payload[offset:])
            if written <= 0:
                raise OSError("short write to MCP collector ledger")
            offset += written
        os.fsync(self._fd)

    def _fsync_directory(self) -> None:
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class _ProtocolObserver:
    def __init__(self, ledger: CallLedger, max_frame_bytes: int):
        if max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be positive")
        self.ledger = ledger
        self.max_frame_bytes = max_frame_bytes
        self._buffers = {"request": bytearray(), "response": bytearray()}
        self._discarding = {"request": False, "response": False}
        self._pending: dict[str, _PendingCall] = {}
        self._other_pending_ids: set[str] = set()
        self._ambiguous_ids: set[str] = set()
        self._completed_ids: set[str] = set()
        self._lock = threading.Lock()
        self.malformed_frames = 0
        self.partial_frames = 0
        self.duplicate_request_ids = 0
        self.missing_responses = 0

    def feed(self, direction: str, chunk: bytes) -> None:
        with self._lock:
            buffer = self._buffers[direction]
            buffer.extend(chunk)
            while True:
                if self._discarding[direction]:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        buffer.clear()
                        return
                    del buffer[: newline + 1]
                    self._discarding[direction] = False
                    continue
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > self.max_frame_bytes:
                        self.malformed_frames += 1
                        buffer.clear()
                        self._discarding[direction] = True
                    return
                frame = bytes(buffer[: newline + 1])
                del buffer[: newline + 1]
                if len(frame) > self.max_frame_bytes:
                    self.malformed_frames += 1
                    continue
                self._observe_frame(direction, frame)

    def finish(self, direction: str) -> None:
        with self._lock:
            if self._buffers[direction] or self._discarding[direction]:
                self.partial_frames += 1
                self._buffers[direction].clear()
                self._discarding[direction] = False

    def finalize_missing(self, returncode: int) -> None:
        with self._lock:
            pending = sorted(
                self._pending.values(),
                key=lambda call: call.request_monotonic_ns,
            )
            self._pending.clear()
            for call in pending:
                self.missing_responses += 1
                self.ledger.append_call(
                    self._call_record(
                        call,
                        status="missing_response",
                        response=None,
                        response_bytes=0,
                        response_unix_ns=None,
                        response_monotonic_ns=None,
                        process_returncode=returncode,
                    )
                )

    def _observe_frame(self, direction: str, frame: bytes) -> None:
        try:
            message = json.loads(frame)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.malformed_frames += 1
            return
        messages = message if isinstance(message, list) else [message]
        if not messages or not all(isinstance(item, dict) for item in messages):
            self.malformed_frames += 1
            return
        for item in messages:
            if direction == "request":
                self._observe_request(item, len(frame))
            else:
                self._observe_response(item, len(frame))

    def _observe_request(self, message: Mapping[str, Any], frame_bytes: int) -> None:
        if "id" not in message or not isinstance(message.get("method"), str):
            return
        key = _id_key(message["id"])
        if message.get("method") != "tools/call":
            if (
                key in self._pending
                or key in self._other_pending_ids
                or key in self._completed_ids
            ):
                self.duplicate_request_ids += 1
                self._ambiguous_ids.add(key)
                self._other_pending_ids.discard(key)
            else:
                self._other_pending_ids.add(key)
            return
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            self.malformed_frames += 1
            return
        if key in self._other_pending_ids:
            self.duplicate_request_ids += 1
            self._ambiguous_ids.add(key)
            self._other_pending_ids.discard(key)
        elif key in self._pending or key in self._completed_ids:
            self.duplicate_request_ids += 1
            return
        now_unix = time.time_ns()
        now_mono = time.monotonic_ns()
        try:
            argument_digest = normalized_argument_digest(
                params.get("arguments", {})
            )
        except ArgumentDigestLimitError:
            self.malformed_frames += 1
            argument_digest = "<unavailable:complexity-limit>"
        self._pending[key] = _PendingCall(
            request_id=message["id"],
            tool=(
                params["name"]
                if params["name"] in COMPUTER_USE_TOOLS
                else "<unrecognized>"
            ),
            argument_digest=argument_digest,
            request_bytes=frame_bytes,
            request_unix_ns=now_unix,
            request_monotonic_ns=now_mono,
        )

    def _observe_response(self, message: Mapping[str, Any], frame_bytes: int) -> None:
        if "id" not in message or ("result" not in message and "error" not in message):
            return
        key = _id_key(message["id"])
        if key in self._ambiguous_ids:
            return
        call = self._pending.pop(key, None)
        if call is None:
            if key in self._other_pending_ids:
                self._other_pending_ids.remove(key)
                return
            if key in self._completed_ids:
                self.malformed_frames += 1
            return
        self._completed_ids.add(key)
        now_unix = time.time_ns()
        now_mono = time.monotonic_ns()
        if "error" in message:
            status = "jsonrpc_error"
        elif isinstance(message.get("result"), dict) and message["result"].get("isError") is True:
            status = "tool_error"
        else:
            status = "completed"
        self.ledger.append_call(
            self._call_record(
                call,
                status=status,
                response=message,
                response_bytes=frame_bytes,
                response_unix_ns=now_unix,
                response_monotonic_ns=now_mono,
                process_returncode=None,
            )
        )

    @staticmethod
    def _call_record(
        call: _PendingCall,
        *,
        status: str,
        response: Mapping[str, Any] | None,
        response_bytes: int,
        response_unix_ns: int | None,
        response_monotonic_ns: int | None,
        process_returncode: int | None,
    ) -> dict[str, Any]:
        result = response.get("result") if isinstance(response, dict) else None
        result = result if isinstance(result, dict) else {}
        metadata = result.get("_meta")
        rpc_error = response.get("error") if isinstance(response, dict) else None
        raw_rpc_error_code = rpc_error.get("code") if isinstance(rpc_error, dict) else None
        rpc_error_code = (
            raw_rpc_error_code
            if isinstance(raw_rpc_error_code, int)
            and not isinstance(raw_rpc_error_code, bool)
            else None
        )
        duration_ms = (
            round((response_monotonic_ns - call.request_monotonic_ns) / 1_000_000, 3)
            if response_monotonic_ns is not None
            else None
        )
        return {
            "tool": call.tool,
            "status": status,
            "request_id_type": type(call.request_id).__name__,
            "argument_digest": call.argument_digest,
            "request_bytes": call.request_bytes,
            "response_bytes": response_bytes,
            "request_unix_ns": call.request_unix_ns,
            "response_unix_ns": response_unix_ns,
            "duration_ms": duration_ms,
            "tool_is_error": result.get("isError") is True,
            "jsonrpc_error": {
                "present": rpc_error is not None,
                "code": rpc_error_code,
            },
            "computer_use_meta": _privacy_safe_meta(metadata),
            "process_returncode": process_returncode,
        }


def _write_all(stream: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is None:
            written = len(view)
        if written <= 0:
            raise BrokenPipeError("short write while relaying MCP stdio")
        view = view[written:]
    stream.flush()


def _read_relay_chunk(stream: BinaryIO, size: int) -> bytes:
    """Read currently available buffered input without waiting to fill `size`."""
    read1 = getattr(stream, "read1", None)
    if callable(read1):
        return read1(size)
    return stream.read(size)


def _command_sha256(command: Sequence[str]) -> str:
    encoded = json.dumps(
        list(command),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_owner_marker(
    path: str | os.PathLike[str],
    *,
    command: Sequence[str],
    child_pid: int | None,
) -> None:
    marker = Path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    command_sha256 = _command_sha256(command)
    payload = {
        "schema_version": "openbench.mcp-process-owner.v1",
        "state": "ready" if child_pid is not None else "starting",
        "collector_pid": os.getpid(),
        "child_pid": child_pid,
        "command_sha256": command_sha256,
    }
    replacing = child_pid is not None
    if replacing:
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectorError(
                f"cannot upgrade MCP owner marker: {exc}"
            ) from exc
        expected = {
            "schema_version": "openbench.mcp-process-owner.v1",
            "state": "starting",
            "collector_pid": os.getpid(),
            "child_pid": None,
            "command_sha256": command_sha256,
        }
        if existing != expected:
            raise CollectorError("MCP owner marker changed before child binding")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{marker.name}.",
        dir=marker.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if replacing:
            os.replace(temporary, marker)
            temporary = ""
        else:
            os.link(temporary, marker)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def collect_stdio(
    command: Sequence[str],
    *,
    ledger_path: str | os.PathLike[str],
    run_id: str,
    trial_id: str,
    stdin: BinaryIO,
    stdout: BinaryIO,
    stderr: BinaryIO,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    owner_path: str | os.PathLike[str] | None = None,
    max_frame_bytes: int = 16 * 1024 * 1024,
) -> CollectionResult:
    """Launch and transparently observe one newline-delimited MCP server."""
    if not command or not all(isinstance(part, str) and part for part in command):
        raise ValueError("command must contain at least one non-empty string")
    ledger = CallLedger(ledger_path, run_id, trial_id)
    observer = _ProtocolObserver(ledger, max_frame_bytes)
    process: subprocess.Popen[bytes] | None = None
    failures: list[BaseException] = []
    failures_lock = threading.Lock()
    input_stopped = threading.Event()
    input_processing_lock = threading.Lock()

    def kill_owned_processes() -> None:
        active_process = process
        if active_process is None:
            return
        try:
            if os.name == "posix":
                os.killpg(active_process.pid, signal.SIGKILL)
            elif active_process.poll() is None:
                active_process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            record_failure(exc, kill_processes=False)

    def record_failure(
        exc: BaseException, *, kill_processes: bool = True
    ) -> None:
        with failures_lock:
            failures.append(exc)
        if kill_processes:
            kill_owned_processes()

    try:
        if owner_path is not None:
            _write_owner_marker(
                owner_path,
                command=command,
                child_pid=None,
            )
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            bufsize=0,
            start_new_session=(os.name == "posix"),
        )
        if owner_path is not None:
            _write_owner_marker(
                owner_path,
                command=command,
                child_pid=process.pid,
            )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        def relay_input() -> None:
            try:
                while chunk := _read_relay_chunk(stdin, 64 * 1024):
                    with input_processing_lock:
                        if input_stopped.is_set():
                            return
                        payload = bytes(chunk)
                        observer.feed("request", payload)
                        _write_all(process.stdin, payload)
            except BrokenPipeError:
                if not input_stopped.is_set() and process.poll() is None:
                    record_failure(BrokenPipeError("MCP child stdin closed unexpectedly"))
            except BaseException as exc:  # propagate worker I/O failures
                if not input_stopped.is_set():
                    record_failure(exc)
            finally:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass

        def relay_output() -> None:
            try:
                while chunk := process.stdout.read(64 * 1024):
                    payload = bytes(chunk)
                    observer.feed("response", payload)
                    _write_all(stdout, payload)
            except BaseException as exc:
                record_failure(exc)
            finally:
                observer.finish("response")

        def relay_stderr() -> None:
            try:
                while chunk := process.stderr.read(64 * 1024):
                    _write_all(stderr, bytes(chunk))
            except BaseException as exc:
                record_failure(exc)

        input_thread = threading.Thread(target=relay_input, daemon=True)
        output_thread = threading.Thread(target=relay_output, daemon=True)
        stderr_thread = threading.Thread(target=relay_stderr, daemon=True)
        for thread in (input_thread, output_thread, stderr_thread):
            thread.start()

        while True:
            try:
                returncode = process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
        input_stopped.set()
        input_incomplete = input_thread.is_alive()
        kill_owned_processes()
        with input_processing_lock:
            pass
        output_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        input_thread.join(timeout=0.1)
        if output_thread.is_alive() or stderr_thread.is_alive():
            raise CollectorError("MCP child relay pipes did not close after process exit")
        observer.finish("request")
        process.stdout.close()
        process.stderr.close()
        observer.finalize_missing(returncode)

        if failures:
            raise CollectorError(
                "MCP relay failed: "
                + "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
            )

        integrity_ok = (
            returncode == 0
            and observer.malformed_frames == 0
            and observer.partial_frames == 0
            and observer.duplicate_request_ids == 0
            and observer.missing_responses == 0
            and not input_incomplete
        )
        summary = {
            "returncode": returncode,
            "integrity_ok": integrity_ok,
            "malformed_frames": observer.malformed_frames,
            "partial_frames": observer.partial_frames,
            "duplicate_request_ids": observer.duplicate_request_ids,
            "missing_responses": observer.missing_responses,
            "input_incomplete": input_incomplete,
        }
        result = ledger.seal(summary)
        verified = verify_ledger(ledger.path)
        if verified.root_hash != result.root_hash:
            raise LedgerIntegrityError("sealed MCP ledger root hash changed after write")
        return result
    except BaseException:
        kill_owned_processes()
        if process is not None and process.poll() is None:
            process.wait()
        ledger.abort()
        raise


def verify_ledger(path: str | os.PathLike[str]) -> LedgerVerification:
    """Fail closed unless the complete ledger and terminal seal are consistent."""
    ledger_path = Path(path)
    try:
        raw = ledger_path.read_bytes()
    except OSError as exc:
        raise LedgerIntegrityError(f"cannot read MCP collector ledger: {exc}") from exc
    if not raw or not raw.endswith(b"\n"):
        raise LedgerIntegrityError("MCP collector ledger is empty or partially written")
    try:
        records = [json.loads(line) for line in raw.splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerIntegrityError("MCP collector ledger contains invalid JSON") from exc
    if not records or records[-1].get("record_type") != "ledger_seal":
        raise LedgerIntegrityError("MCP collector ledger has no terminal seal")
    if any(record.get("record_type") == "ledger_seal" for record in records[:-1]):
        raise LedgerIntegrityError("MCP collector ledger contains records after a seal")

    calls = records[:-1]
    seal = records[-1]
    root_hash = EMPTY_ROOT_HASH
    run_id = seal.get("run_id")
    trial_id = seal.get("trial_id")
    if not isinstance(run_id, str) or not isinstance(trial_id, str):
        raise LedgerIntegrityError("MCP collector seal has invalid trial identity")
    for sequence, record in enumerate(calls, 1):
        if record.get("record_type") != "tool_call":
            raise LedgerIntegrityError("MCP collector ledger has an unknown record type")
        if (
            record.get("run_id") != run_id
            or record.get("trial_id") != trial_id
            or record.get("sequence") != sequence
            or record.get("previous_hash") != root_hash
        ):
            raise LedgerIntegrityError("MCP collector ledger chain metadata disagrees")
        unhashed = dict(record)
        recorded_hash = unhashed.pop("record_hash", None)
        actual_hash = hashlib.sha256(_canonical_bytes(unhashed)).hexdigest()
        if recorded_hash != actual_hash:
            raise LedgerIntegrityError("MCP collector ledger record hash disagrees")
        root_hash = actual_hash

    summary = seal.get("summary")
    if not isinstance(summary, dict):
        raise LedgerIntegrityError("MCP collector seal has no summary")
    unhashed_seal = dict(seal)
    recorded_seal_hash = unhashed_seal.pop("seal_hash", None)
    actual_seal_hash = hashlib.sha256(_canonical_bytes(unhashed_seal)).hexdigest()
    if recorded_seal_hash != actual_seal_hash:
        raise LedgerIntegrityError("MCP collector terminal seal hash disagrees")
    if (
        seal.get("call_count") != len(calls)
        or seal.get("last_sequence") != len(calls)
        or seal.get("root_hash") != root_hash
    ):
        raise LedgerIntegrityError("MCP collector seal disagrees with its records")
    integrity_ok = summary.get("integrity_ok")
    if not isinstance(integrity_ok, bool):
        raise LedgerIntegrityError("MCP collector summary has invalid integrity state")
    expected_summary_fields = {
        "returncode": int,
        "malformed_frames": int,
        "partial_frames": int,
        "duplicate_request_ids": int,
        "missing_responses": int,
    }
    for field, expected_type in expected_summary_fields.items():
        value = summary.get(field)
        if (
            not isinstance(value, expected_type)
            or isinstance(value, bool)
            or (field != "returncode" and value < 0)
        ):
            raise LedgerIntegrityError(
                f"MCP collector summary has invalid {field}"
            )
    input_incomplete = summary.get("input_incomplete")
    if not isinstance(input_incomplete, bool):
        raise LedgerIntegrityError(
            "MCP collector summary has invalid input_incomplete"
        )
    expected_integrity = (
        summary["returncode"] == 0
        and summary["malformed_frames"] == 0
        and summary["partial_frames"] == 0
        and summary["duplicate_request_ids"] == 0
        and summary["missing_responses"] == 0
        and not input_incomplete
    )
    if integrity_ok != expected_integrity:
        raise LedgerIntegrityError("MCP collector summary integrity state disagrees")
    return LedgerVerification(
        run_id=run_id,
        trial_id=trial_id,
        call_count=len(calls),
        root_hash=root_hash,
        integrity_ok=integrity_ok,
        summary=summary,
    )
