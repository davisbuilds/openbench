"""Transparent MCP stdio relay with a privacy-safe, sealed call ledger.

The collector observes newline-delimited JSON-RPC while relaying the original
bytes unchanged. It is intentionally not a JSON-RPC endpoint: messages outside
``tools/call`` request/response pairs are never interpreted or rewritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence


EMPTY_ROOT_HASH = hashlib.sha256(b"").hexdigest()
META_KEYS = {
    "error": "computer-use-mcp/error",
    "outcome": "computer-use-mcp/outcome",
    "focus": "computer-use-mcp/focus",
    "delivery": "computer-use-mcp/delivery",
}


class CollectorError(RuntimeError):
    """Raised when collection cannot preserve or durably account for traffic."""


class LedgerIntegrityError(CollectorError):
    """Raised when a collector ledger is incomplete or fails its hash chain."""


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


def _argument_shape(value: Any) -> Any:
    """Normalize values into non-reversible type and size information."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string", "length": _length_bucket(len(value))}
    if isinstance(value, list):
        return {
            "type": "array",
            "length": _length_bucket(len(value)),
            "items": [_argument_shape(item) for item in value],
        }
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": {
                str(key): _argument_shape(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            },
        }
    return {"type": type(value).__name__}


def normalized_argument_digest(arguments: Any) -> str:
    """Digest argument structure without retaining scalar argument values."""
    return "sha256:" + hashlib.sha256(
        _canonical_bytes(_argument_shape(arguments))
    ).hexdigest()


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
                "summary": dict(summary),
            }
            terminal["seal_hash"] = hashlib.sha256(
                _canonical_bytes(terminal)
            ).hexdigest()
            self._append_durable(terminal)
            os.close(self._fd)
            self._sealed = True
            self._fsync_directory()
            return CollectionResult(
                returncode=int(summary["returncode"]),
                ledger_path=self.path,
                call_count=self._call_count,
                root_hash=self._root_hash,
                integrity_ok=bool(summary["integrity_ok"]),
                malformed_frames=int(summary["malformed_frames"]),
                partial_frames=int(summary["partial_frames"]),
                missing_responses=int(summary["missing_responses"]),
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
        self._oversized = {"request": False, "response": False}
        self._pending: dict[str, _PendingCall] = {}
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
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > self.max_frame_bytes:
                        self._oversized[direction] = True
                    return
                frame = bytes(buffer[: newline + 1])
                del buffer[: newline + 1]
                oversized = self._oversized[direction] or len(frame) > self.max_frame_bytes
                self._oversized[direction] = False
                if oversized:
                    self.malformed_frames += 1
                    continue
                self._observe_frame(direction, frame)

    def finish(self, direction: str) -> None:
        with self._lock:
            if self._buffers[direction]:
                self.partial_frames += 1
                self._buffers[direction].clear()
                self._oversized[direction] = False

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
        if message.get("method") != "tools/call" or "id" not in message:
            return
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            self.malformed_frames += 1
            return
        key = _id_key(message["id"])
        if key in self._pending:
            self.duplicate_request_ids += 1
            return
        now_unix = time.time_ns()
        now_mono = time.monotonic_ns()
        self._pending[key] = _PendingCall(
            request_id=message["id"],
            tool=params["name"],
            argument_digest=normalized_argument_digest(params.get("arguments", {})),
            request_bytes=frame_bytes,
            request_unix_ns=now_unix,
            request_monotonic_ns=now_mono,
        )

    def _observe_response(self, message: Mapping[str, Any], frame_bytes: int) -> None:
        if "id" not in message or ("result" not in message and "error" not in message):
            return
        call = self._pending.pop(_id_key(message["id"]), None)
        if call is None:
            return
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
        metadata = metadata if isinstance(metadata, dict) else {}
        rpc_error = response.get("error") if isinstance(response, dict) else None
        rpc_error_code = rpc_error.get("code") if isinstance(rpc_error, dict) else None
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
            "computer_use_meta": {
                short: metadata.get(source)
                for short, source in META_KEYS.items()
            },
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

    def record_failure(exc: BaseException) -> None:
        with failures_lock:
            failures.append(exc)

    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            bufsize=0,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        def relay_input() -> None:
            try:
                while chunk := stdin.read(64 * 1024):
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
        output_thread = threading.Thread(target=relay_output)
        stderr_thread = threading.Thread(target=relay_stderr)
        for thread in (input_thread, output_thread, stderr_thread):
            thread.start()

        returncode = process.wait()
        input_stopped.set()
        output_thread.join()
        stderr_thread.join()
        input_thread.join(timeout=0.1)
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
        )
        summary = {
            "returncode": returncode,
            "integrity_ok": integrity_ok,
            "malformed_frames": observer.malformed_frames,
            "partial_frames": observer.partial_frames,
            "duplicate_request_ids": observer.duplicate_request_ids,
            "missing_responses": observer.missing_responses,
        }
        result = ledger.seal(summary)
        verified = verify_ledger(ledger.path)
        if verified.root_hash != result.root_hash:
            raise LedgerIntegrityError("sealed MCP ledger root hash changed after write")
        return result
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
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
    expected_integrity = (
        summary["returncode"] == 0
        and summary["malformed_frames"] == 0
        and summary["partial_frames"] == 0
        and summary["duplicate_request_ids"] == 0
        and summary["missing_responses"] == 0
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
