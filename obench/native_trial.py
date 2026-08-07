"""Strict importer for sealed native macOS benchmark trial bundles.

This module validates evidence produced by a native macOS runner. It does not
run an app, synthesize Harbor data, or publish results. The bundle manifest is
the complete file inventory and every semantic evidence object is also bound to
the same trial and immutable lock.
"""

from __future__ import annotations

from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .atif import SCHEMA_VERSION as ATIF_SCHEMA_VERSION, validate_trajectory
from .mcp_stdio_collector import (
    LedgerIntegrityError as MCPCollectorIntegrityError,
    verify_ledger as verify_mcp_collector_ledger,
)
from .run import ROW_FIELDS, make_run_id


BUNDLE_SCHEMA_VERSION = "openbench.native-macos-trial.v1"
TASK_SIDECAR_SCHEMA_VERSION = "openbench.native-task.v1"
NATIVE_SIDECAR_SCHEMA_VERSION = "openbench.native-sidecar.v1"
LEDGER_SCHEMA_VERSION = "openbench.native-ledger.v1"

MANIFEST_PATH = "manifest.json"
REQUIRED_FILES = frozenset(
    {
        "lock.json",
        "result.json",
        "agent/trajectory.json",
        "verifier/reward.json",
        "verifier/evidence.json",
        "artifacts/manifest.json",
        "mcp/ledger.jsonl",
        "focus/ledger.jsonl",
        "focus/seal.json",
        "task/task.json",
        "task/native.json",
    }
)
PROXY_FILES = frozenset({"proxy/ledger.jsonl", "proxy/seal.json"})
TERMINAL_STATUSES = frozenset(
    {"timeout", "error", "preflight_failed", "retry_exhausted"}
)
ALLOWED_STATUSES = TERMINAL_STATUSES | {"completed"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SECRET_RE = re.compile(
    r"\b(?:"
    r"(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|"
    r"gh[opsur]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|"
    r"hf_[A-Za-z0-9]{20,}"
    r")\b"
)
_PRIVATE_PATH_RE = re.compile(r"(?:^|[\s\"'])(?:/Users/|/home/|file:///)")
_TRANSCRIPT_NAME_RE = re.compile(
    r"(?:^|/)(?:transcripts?|raw[-_.]?output|session[-_.]?log)(?:/|\.|$)",
    re.IGNORECASE,
)


class NativeTrialError(ValueError):
    """Raised when native trial evidence is incomplete or contradictory."""


def _fail(location: str, message: str) -> NativeTrialError:
    return NativeTrialError(f"{location}: {message}")


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(f"duplicate object key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _read_json(path: Path, location: str) -> dict[str, Any]:
    _require_regular_file(path, location)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _fail(location, f"invalid JSON: {exc}") from exc
    return _object(value, location)


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(location, "expected an object")
    return value


def _array(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise _fail(location, "expected an array")
    return value


def _exact_fields(value: Mapping[str, Any], fields: set[str], location: str) -> None:
    missing = sorted(fields - set(value))
    extra = sorted(set(value) - fields)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected fields: {', '.join(extra)}")
        raise _fail(location, "; ".join(details))


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise _fail(location, "expected a non-empty trimmed string")
    return value


def _optional_string(value: Any, location: str) -> str | None:
    return None if value is None else _string(value, location)


def _integer(value: Any, location: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _fail(location, f"expected an integer >= {minimum}")
    return value


def _number(
    value: Any,
    location: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
        or (maximum is not None and float(value) > maximum)
    ):
        limit = "" if maximum is None else f" and <= {maximum}"
        raise _fail(location, f"expected a finite number >= {minimum}{limit}")
    return float(value)


def _optional_integer(value: Any, location: str) -> int | None:
    return None if value is None else _integer(value, location)


def _digest(value: Any, location: str) -> str:
    value = _string(value, location)
    if _SHA256_RE.fullmatch(value) is None:
        raise _fail(location, "expected a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, location: str) -> datetime:
    text = _string(value, location)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _fail(location, "expected an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _fail(location, "timestamp must include an offset")
    return parsed


def _relative_path(value: Any, location: str) -> str:
    text = _string(value, location)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or text != path.as_posix()
        or any(part in ("", ".", "..") for part in path.parts)
        or "\\" in text
    ):
        raise _fail(location, "expected a normalized safe relative POSIX path")
    return text


def _require_regular_file(path: Path, location: str) -> None:
    if path.is_symlink():
        raise _fail(location, "symlinks are not accepted")
    if not path.is_file():
        raise _fail(location, "required regular file is missing")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reject_private_text(text: str, location: str) -> None:
    if _EMAIL_RE.search(text):
        raise _fail(location, "email address is not accepted in importable evidence")
    if _SECRET_RE.search(text):
        raise _fail(location, "credential-like token is not accepted")
    if _PRIVATE_PATH_RE.search(text):
        raise _fail(location, "absolute home/file path is not accepted")


def _scan_privacy(value: Any, location: str) -> None:
    if isinstance(value, str):
        _reject_private_text(value, location)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_privacy(item, f"{location}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_privacy(item, f"{location}.{key}")


def _reject_harbor_shape(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized.startswith("harbor_") or normalized in {
                "harbor",
                "job_lock",
                "trial_lock",
                "task_checksum",
                "verifier_environment_mode",
            }:
                raise _fail(location, f"synthetic Harbor field {key!r} is not accepted")
            _reject_harbor_shape(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_harbor_shape(item, f"{location}[{index}]")
    elif isinstance(value, str) and value.lower() in {"harbor", "harbor_job"}:
        raise _fail(location, "synthetic Harbor identity is not accepted")


def _inventory(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise _fail("bundle", f"symlink directory is not accepted: {path}")
        for name in names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            _relative_path(relative, f"bundle file {relative!r}")
            _require_regular_file(path, f"bundle file {relative!r}")
            files[relative] = path
    return files


def _validate_manifest(root: Path) -> tuple[str, dict[str, Path]]:
    manifest = _read_json(root / MANIFEST_PATH, "manifest")
    _exact_fields(
        manifest,
        {"schema_version", "trial_id", "lock_sha256", "result_sha256", "files"},
        "manifest",
    )
    if manifest["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise _fail("manifest.schema_version", f"expected {BUNDLE_SCHEMA_VERSION!r}")
    trial_id = _string(manifest["trial_id"], "manifest.trial_id")
    declared: dict[str, tuple[str, int]] = {}
    for index, raw_entry in enumerate(_array(manifest["files"], "manifest.files")):
        location = f"manifest.files[{index}]"
        entry = _object(raw_entry, location)
        _exact_fields(entry, {"path", "sha256", "size"}, location)
        path = _relative_path(entry["path"], f"{location}.path")
        if path == MANIFEST_PATH or path in declared:
            raise _fail(f"{location}.path", "duplicate or self-referential path")
        declared[path] = (
            _digest(entry["sha256"], f"{location}.sha256"),
            _integer(entry["size"], f"{location}.size"),
        )
    actual = _inventory(root)
    actual_without_manifest = {key: value for key, value in actual.items() if key != MANIFEST_PATH}
    if set(declared) != set(actual_without_manifest):
        missing = sorted(set(declared) - set(actual_without_manifest))
        undeclared = sorted(set(actual_without_manifest) - set(declared))
        raise _fail(
            "manifest.files",
            f"inventory mismatch; missing={missing!r}, undeclared={undeclared!r}",
        )
    if not REQUIRED_FILES <= set(declared):
        raise _fail(
            "manifest.files",
            f"missing required evidence: {sorted(REQUIRED_FILES - set(declared))!r}",
        )
    has_proxy = bool(PROXY_FILES & set(declared))
    if has_proxy and not PROXY_FILES <= set(declared):
        raise _fail("manifest.files", "counting-proxy ledger and seal must appear together")
    unexpected = sorted(
        path
        for path in declared
        if path not in REQUIRED_FILES
        and path not in PROXY_FILES
        and not path.startswith("artifacts/final-state/")
    )
    if unexpected:
        raise _fail("manifest.files", f"unexpected evidence paths: {unexpected!r}")
    for path, (expected_digest, expected_size) in declared.items():
        actual_path = actual_without_manifest[path]
        if actual_path.stat().st_size != expected_size:
            raise _fail(path, "size does not match manifest")
        if _sha256_file(actual_path) != expected_digest:
            raise _fail(path, "SHA-256 does not match manifest")
        if _TRANSCRIPT_NAME_RE.search(path):
            raise _fail(path, "raw transcript-like evidence is local-only")
    if _digest(manifest["lock_sha256"], "manifest.lock_sha256") != declared["lock.json"][0]:
        raise _fail("manifest.lock_sha256", "does not match lock.json")
    if _digest(manifest["result_sha256"], "manifest.result_sha256") != declared["result.json"][0]:
        raise _fail("manifest.result_sha256", "does not match result.json")
    return trial_id, actual


def _bound_json(
    root: Path,
    relative: str,
    *,
    trial_id: str,
    lock_sha256: str,
    fields: set[str],
) -> dict[str, Any]:
    value = _read_json(root / relative, relative)
    _exact_fields(value, fields | {"schema_version", "trial_id", "lock_sha256"}, relative)
    if value["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise _fail(f"{relative}.schema_version", "does not match bundle schema")
    if value["trial_id"] != trial_id:
        raise _fail(f"{relative}.trial_id", "does not match manifest")
    if value["lock_sha256"] != lock_sha256:
        raise _fail(f"{relative}.lock_sha256", "does not match immutable lock")
    return value


def _validate_lock(root: Path, trial_id: str) -> tuple[dict[str, Any], str]:
    lock = _read_json(root / "lock.json", "lock")
    _exact_fields(
        lock,
        {
            "schema_version",
            "trial_id",
            "created_at",
            "task",
            "native_sidecar",
            "harness",
            "model",
            "mcp",
            "environment",
            "budget",
            "evidence",
        },
        "lock",
    )
    if lock["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise _fail("lock.schema_version", f"expected {BUNDLE_SCHEMA_VERSION!r}")
    if lock["trial_id"] != trial_id:
        raise _fail("lock.trial_id", "does not match manifest")
    _timestamp(lock["created_at"], "lock.created_at")

    task = _object(lock["task"], "lock.task")
    _exact_fields(task, {"name", "sidecar_path", "sidecar_sha256"}, "lock.task")
    _string(task["name"], "lock.task.name")
    if _relative_path(task["sidecar_path"], "lock.task.sidecar_path") != "task/task.json":
        raise _fail("lock.task.sidecar_path", "must be 'task/task.json'")
    _digest(task["sidecar_sha256"], "lock.task.sidecar_sha256")

    native = _object(lock["native_sidecar"], "lock.native_sidecar")
    _exact_fields(native, {"path", "sha256"}, "lock.native_sidecar")
    if _relative_path(native["path"], "lock.native_sidecar.path") != "task/native.json":
        raise _fail("lock.native_sidecar.path", "must be 'task/native.json'")
    _digest(native["sha256"], "lock.native_sidecar.sha256")

    harness = _object(lock["harness"], "lock.harness")
    _exact_fields(harness, {"name", "version", "version_source"}, "lock.harness")
    for field in harness:
        _string(harness[field], f"lock.harness.{field}")

    model = _object(lock["model"], "lock.model")
    _exact_fields(model, {"name", "provider", "revision"}, "lock.model")
    for field in model:
        _string(model[field], f"lock.model.{field}")

    mcp = _object(lock["mcp"], "lock.mcp")
    _exact_fields(
        mcp,
        {"name", "version", "transport", "server_sha256", "collector_run_id"},
        "lock.mcp",
    )
    for field in ("name", "version", "transport", "collector_run_id"):
        _string(mcp[field], f"lock.mcp.{field}")
    _digest(mcp["server_sha256"], "lock.mcp.server_sha256")

    environment = _object(lock["environment"], "lock.environment")
    _exact_fields(
        environment,
        {"platform", "os", "architecture", "hardware_model", "app", "display", "preflight"},
        "lock.environment",
    )
    if environment["platform"] != "macos":
        raise _fail("lock.environment.platform", "must be 'macos'")
    os_identity = _object(environment["os"], "lock.environment.os")
    _exact_fields(os_identity, {"version", "build"}, "lock.environment.os")
    for field in os_identity:
        _string(os_identity[field], f"lock.environment.os.{field}")
    _string(environment["architecture"], "lock.environment.architecture")
    _string(environment["hardware_model"], "lock.environment.hardware_model")
    app = _object(environment["app"], "lock.environment.app")
    _exact_fields(
        app,
        {"bundle_id", "version", "build", "code_signature_sha256"},
        "lock.environment.app",
    )
    for field in ("bundle_id", "version", "build"):
        _string(app[field], f"lock.environment.app.{field}")
    _digest(app["code_signature_sha256"], "lock.environment.app.code_signature_sha256")
    display = _object(environment["display"], "lock.environment.display")
    _exact_fields(
        display,
        {"width_px", "height_px", "scale_factor", "color_space"},
        "lock.environment.display",
    )
    _integer(display["width_px"], "lock.environment.display.width_px", minimum=1)
    _integer(display["height_px"], "lock.environment.display.height_px", minimum=1)
    _number(display["scale_factor"], "lock.environment.display.scale_factor", minimum=0.1)
    _string(display["color_space"], "lock.environment.display.color_space")
    preflight = _object(environment["preflight"], "lock.environment.preflight")
    _exact_fields(
        preflight,
        {
            "accessibility",
            "screen_recording",
            "app_installed",
            "display_stable",
            "focus_monitor_ready",
        },
        "lock.environment.preflight",
    )
    if any(not isinstance(value, bool) for value in preflight.values()):
        raise _fail("lock.environment.preflight", "all preflight values must be booleans")

    budget = _object(lock["budget"], "lock.budget")
    _exact_fields(budget, {"timeout_s", "max_retries"}, "lock.budget")
    _number(budget["timeout_s"], "lock.budget.timeout_s", minimum=0.001)
    _integer(budget["max_retries"], "lock.budget.max_retries")

    evidence = _object(lock["evidence"], "lock.evidence")
    _exact_fields(evidence, {"proxy_required"}, "lock.evidence")
    if not isinstance(evidence["proxy_required"], bool):
        raise _fail("lock.evidence.proxy_required", "expected a boolean")

    _scan_privacy(lock, "lock")
    _reject_harbor_shape(lock, "lock")
    return lock, _sha256_file(root / "lock.json")


def _validate_sidecars(
    root: Path, lock: dict[str, Any], trial_id: str, lock_sha256: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    task = _read_json(root / "task/task.json", "task/task.json")
    _exact_fields(
        task,
        {
            "schema_version",
            "trial_id",
            "task_id",
            "task_content_sha256",
            "instruction_sha256",
            "verifier_sha256",
        },
        "task/task.json",
    )
    if task["schema_version"] != TASK_SIDECAR_SCHEMA_VERSION:
        raise _fail("task/task.json.schema_version", "unexpected task sidecar schema")
    if task["trial_id"] != trial_id or task["task_id"] != lock["task"]["name"]:
        raise _fail("task/task.json", "task identity does not match lock")
    for field in ("task_content_sha256", "instruction_sha256", "verifier_sha256"):
        _digest(task[field], f"task/task.json.{field}")
    if _sha256_file(root / "task/task.json") != lock["task"]["sidecar_sha256"]:
        raise _fail("task/task.json", "digest does not match lock")

    native = _read_json(root / "task/native.json", "task/native.json")
    _exact_fields(
        native,
        {
            "schema_version",
            "trial_id",
            "task_id",
            "app_bundle_id",
            "reset_contract_sha256",
            "success_contract_sha256",
            "final_state_allowlist",
        },
        "task/native.json",
    )
    if native["schema_version"] != NATIVE_SIDECAR_SCHEMA_VERSION:
        raise _fail("task/native.json.schema_version", "unexpected native sidecar schema")
    if native["trial_id"] != trial_id or native["task_id"] != lock["task"]["name"]:
        raise _fail("task/native.json", "task identity does not match lock")
    if native["app_bundle_id"] != lock["environment"]["app"]["bundle_id"]:
        raise _fail("task/native.json.app_bundle_id", "does not match locked app")
    for field in ("reset_contract_sha256", "success_contract_sha256"):
        _digest(native[field], f"task/native.json.{field}")
    allowlist = [
        _relative_path(value, f"task/native.json.final_state_allowlist[{index}]")
        for index, value in enumerate(
            _array(native["final_state_allowlist"], "task/native.json.final_state_allowlist")
        )
    ]
    if len(allowlist) != len(set(allowlist)):
        raise _fail("task/native.json.final_state_allowlist", "duplicate path")
    if _sha256_file(root / "task/native.json") != lock["native_sidecar"]["sha256"]:
        raise _fail("task/native.json", "digest does not match lock")
    _scan_privacy(task, "task/task.json")
    _scan_privacy(native, "task/native.json")
    return task, native


def _verify_ledger(
    root: Path,
    prefix: str,
    *,
    trial_id: str,
    lock_sha256: str,
    allowed_kinds: set[str],
) -> list[dict[str, Any]]:
    ledger_path = root / prefix / "ledger.jsonl"
    records: list[dict[str, Any]] = []
    previous_hash = "0" * 64
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(f"{prefix}/ledger.jsonl", f"cannot read ledger: {exc}") from exc
    if not lines or any(not line.strip() for line in lines):
        raise _fail(f"{prefix}/ledger.jsonl", "must contain non-empty JSONL records")
    for index, line in enumerate(lines, 1):
        location = f"{prefix}/ledger.jsonl:{index}"
        try:
            record = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise _fail(location, f"invalid JSON: {exc}") from exc
        record = _object(record, location)
        _exact_fields(
            record,
            {
                "schema_version",
                "trial_id",
                "lock_sha256",
                "sequence",
                "kind",
                "timestamp",
                "payload",
                "previous_hash",
                "record_hash",
            },
            location,
        )
        if record["schema_version"] != LEDGER_SCHEMA_VERSION:
            raise _fail(f"{location}.schema_version", "unexpected ledger schema")
        if record["trial_id"] != trial_id or record["lock_sha256"] != lock_sha256:
            raise _fail(location, "ledger identity does not match trial lock")
        if _integer(record["sequence"], f"{location}.sequence", minimum=1) != index:
            raise _fail(f"{location}.sequence", "must be contiguous and one-based")
        if record["kind"] not in allowed_kinds:
            raise _fail(f"{location}.kind", "unexpected record kind")
        _timestamp(record["timestamp"], f"{location}.timestamp")
        _object(record["payload"], f"{location}.payload")
        if record["previous_hash"] != previous_hash:
            raise _fail(f"{location}.previous_hash", "does not continue hash chain")
        expected_hash = _canonical_digest(
            {key: value for key, value in record.items() if key != "record_hash"}
        )
        if record["record_hash"] != expected_hash:
            raise _fail(f"{location}.record_hash", "does not match record content")
        previous_hash = expected_hash
        _scan_privacy(record["payload"], f"{location}.payload")
        _reject_harbor_shape(record, location)
        records.append(record)

    seal = _read_json(root / prefix / "seal.json", f"{prefix}/seal.json")
    _exact_fields(
        seal,
        {
            "schema_version",
            "trial_id",
            "lock_sha256",
            "record_count",
            "last_sequence",
            "root_hash",
            "ledger_sha256",
        },
        f"{prefix}/seal.json",
    )
    if seal["schema_version"] != LEDGER_SCHEMA_VERSION:
        raise _fail(f"{prefix}/seal.json.schema_version", "unexpected ledger schema")
    expected_seal = {
        "trial_id": trial_id,
        "lock_sha256": lock_sha256,
        "record_count": len(records),
        "last_sequence": len(records),
        "root_hash": previous_hash,
        "ledger_sha256": _sha256_file(ledger_path),
    }
    for key, expected in expected_seal.items():
        if seal[key] != expected:
            raise _fail(f"{prefix}/seal.json.{key}", "does not match sealed ledger")
    return records


def _verify_mcp_ledger(
    root: Path,
    *,
    trial_id: str,
    collector_run_id: str,
    started: datetime,
    finished: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any], Any]:
    path = root / "mcp/ledger.jsonl"
    _require_regular_file(path, "mcp/ledger.jsonl")
    try:
        verified = verify_mcp_collector_ledger(path)
    except MCPCollectorIntegrityError as exc:
        raise _fail("mcp/ledger.jsonl", str(exc)) from exc
    if verified.trial_id != trial_id or verified.run_id != collector_run_id:
        raise _fail(
            "mcp/ledger.jsonl",
            "collector run/trial identity does not match immutable lock",
        )
    if not verified.integrity_ok:
        raise _fail("mcp/ledger.jsonl", "collector terminal seal is not clean")
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        location = f"mcp/ledger.jsonl:{line_number}"
        try:
            record = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise _fail(location, f"invalid JSON: {exc}") from exc
        record = _object(record, location)
        _scan_privacy(record, location)
        _reject_harbor_shape(record, location)
        records.append(record)
    calls = records[:-1]
    seal = records[-1]
    if len(calls) != verified.call_count:
        raise _fail("mcp/ledger.jsonl", "collector verifier count disagrees")
    start_ns = int(started.timestamp() * 1_000_000_000)
    finish_ns = int(finished.timestamp() * 1_000_000_000)
    for index, call in enumerate(calls, 1):
        request_ns = _integer(
            call.get("request_unix_ns"),
            f"mcp/ledger.jsonl:{index}.request_unix_ns",
        )
        response_ns = call.get("response_unix_ns")
        if response_ns is not None:
            response_ns = _integer(
                response_ns,
                f"mcp/ledger.jsonl:{index}.response_unix_ns",
            )
        if (
            request_ns < start_ns
            or request_ns > finish_ns
            or (response_ns is not None and (response_ns < request_ns or response_ns > finish_ns))
        ):
            raise _fail(
                f"mcp/ledger.jsonl:{index}",
                "collector timestamps fall outside trial timing",
            )
    return calls, seal, verified


def _validate_trajectory(
    root: Path,
    *,
    trial_id: str,
    lock: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int | None], int]:
    trajectory = _read_json(root / "agent/trajectory.json", "agent/trajectory.json")
    errors = validate_trajectory(trajectory)
    if errors:
        raise _fail("agent/trajectory.json", "; ".join(errors))
    if trajectory.get("schema_version") != ATIF_SCHEMA_VERSION:
        raise _fail("agent/trajectory.json.schema_version", f"expected {ATIF_SCHEMA_VERSION}")
    if trajectory.get("trajectory_id") != trial_id:
        raise _fail("agent/trajectory.json.trajectory_id", "does not match trial")
    agent = _object(trajectory.get("agent"), "agent/trajectory.json.agent")
    expected_agent = lock["harness"]
    if agent.get("name") != expected_agent["name"] or agent.get("version") != expected_agent["version"]:
        raise _fail("agent/trajectory.json.agent", "does not match locked harness")
    if agent.get("model_name") not in (None, lock["model"]["name"]):
        raise _fail("agent/trajectory.json.agent.model_name", "does not match locked model")
    for index, step in enumerate(trajectory["steps"]):
        if step.get("source") == "agent" and step.get("model_name") not in (
            None,
            lock["model"]["name"],
        ):
            raise _fail(
                f"agent/trajectory.json.steps[{index}].model_name",
                "does not match locked model",
            )
    metrics = _object(trajectory.get("final_metrics", {}), "agent/trajectory.json.final_metrics")
    usage = {
        "input": _optional_integer(
            metrics.get("total_prompt_tokens"),
            "agent/trajectory.json.final_metrics.total_prompt_tokens",
        ),
        "cached": _optional_integer(
            metrics.get("total_cached_tokens"),
            "agent/trajectory.json.final_metrics.total_cached_tokens",
        ),
        "output": _optional_integer(
            metrics.get("total_completion_tokens"),
            "agent/trajectory.json.final_metrics.total_completion_tokens",
        ),
    }
    values = tuple(usage.values())
    if any(value is None for value in values) and not all(value is None for value in values):
        raise _fail("agent/trajectory.json.final_metrics", "token usage is partial")
    if usage["input"] is not None and usage["cached"] > usage["input"]:
        raise _fail("agent/trajectory.json.final_metrics", "cached tokens exceed input tokens")
    turns = sum(1 for step in trajectory["steps"] if step.get("source") == "agent")
    _scan_privacy(trajectory, "agent/trajectory.json")
    _reject_harbor_shape(trajectory, "agent/trajectory.json")
    return trajectory, usage, turns


def _validate_artifacts(
    root: Path,
    *,
    trial_id: str,
    lock_sha256: str,
    native_sidecar: dict[str, Any],
) -> str:
    manifest = _bound_json(
        root,
        "artifacts/manifest.json",
        trial_id=trial_id,
        lock_sha256=lock_sha256,
        fields={"reviewed", "contains_sensitive_data", "artifacts"},
    )
    if manifest["reviewed"] is not True or manifest["contains_sensitive_data"] is not False:
        raise _fail(
            "artifacts/manifest.json",
            "final-state artifacts require reviewed=true and contains_sensitive_data=false",
        )
    declared: list[str] = []
    aggregate: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(_array(manifest["artifacts"], "artifacts/manifest.json.artifacts")):
        location = f"artifacts/manifest.json.artifacts[{index}]"
        entry = _object(raw_entry, location)
        _exact_fields(entry, {"path", "sha256", "size", "media_type", "classification"}, location)
        path = _relative_path(entry["path"], f"{location}.path")
        if not path.startswith("artifacts/final-state/"):
            raise _fail(f"{location}.path", "must be below artifacts/final-state/")
        if _TRANSCRIPT_NAME_RE.search(path):
            raise _fail(f"{location}.path", "transcript-like artifacts are local-only")
        if path in declared:
            raise _fail(f"{location}.path", "duplicate artifact")
        if entry["classification"] != "public_evidence":
            raise _fail(f"{location}.classification", "must be 'public_evidence'")
        _string(entry["media_type"], f"{location}.media_type")
        expected_digest = _digest(entry["sha256"], f"{location}.sha256")
        expected_size = _integer(entry["size"], f"{location}.size")
        artifact_path = root / path
        _require_regular_file(artifact_path, path)
        if artifact_path.stat().st_size != expected_size or _sha256_file(artifact_path) != expected_digest:
            raise _fail(path, "artifact bytes do not match artifact manifest")
        if expected_size <= 1024 * 1024 and (
            entry["media_type"].startswith("text/") or entry["media_type"] == "application/json"
        ):
            try:
                _reject_private_text(artifact_path.read_text(encoding="utf-8"), path)
            except UnicodeDecodeError as exc:
                raise _fail(path, "declared text artifact is not UTF-8") from exc
        declared.append(path)
        aggregate.append({"path": path, "sha256": expected_digest, "size": expected_size})
    if not declared:
        raise _fail("artifacts/manifest.json.artifacts", "at least one final-state artifact is required")
    final_root = root / "artifacts/final-state"
    actual_final = {
        path.relative_to(root).as_posix()
        for path in final_root.rglob("*")
        if path.is_file()
    }
    if set(declared) != actual_final:
        raise _fail(
            "artifacts/manifest.json.artifacts",
            "does not inventory every final-state file exactly",
        )
    if set(declared) != set(native_sidecar["final_state_allowlist"]):
        raise _fail(
            "artifacts/manifest.json.artifacts",
            "paths do not match native sidecar final-state allowlist",
        )
    return _canonical_digest(aggregate)


def _validate_result_and_verifier(
    root: Path,
    *,
    trial_id: str,
    lock_sha256: str,
    lock: dict[str, Any],
) -> tuple[dict[str, Any], datetime, datetime]:
    result = _bound_json(
        root,
        "result.json",
        trial_id=trial_id,
        lock_sha256=lock_sha256,
        fields={
            "status",
            "attempts",
            "retry_count",
            "timeout_s",
            "started_at",
            "finished_at",
            "timings",
            "outcome",
            "mcp_event_count",
            "focus_event_count",
        },
    )
    status = result["status"]
    if status not in ALLOWED_STATUSES:
        raise _fail("result.status", f"expected one of {sorted(ALLOWED_STATUSES)!r}")
    attempts = _integer(result["attempts"], "result.attempts", minimum=1)
    retries = _integer(result["retry_count"], "result.retry_count")
    if attempts != retries + 1 or retries > lock["budget"]["max_retries"]:
        raise _fail("result.retry_count", "is inconsistent with attempts or locked budget")
    timeout_s = _number(result["timeout_s"], "result.timeout_s", minimum=0.001)
    if timeout_s != float(lock["budget"]["timeout_s"]):
        raise _fail("result.timeout_s", "does not match locked budget")
    started = _timestamp(result["started_at"], "result.started_at")
    finished = _timestamp(result["finished_at"], "result.finished_at")
    if finished < started:
        raise _fail("result.finished_at", "precedes started_at")
    timings = _object(result["timings"], "result.timings")
    _exact_fields(timings, {"env_setup_s", "agent_s", "verifier_s", "total_s"}, "result.timings")
    for field in timings:
        _number(timings[field], f"result.timings.{field}")
    elapsed = (finished - started).total_seconds()
    if abs(float(timings["total_s"]) - elapsed) > 0.001:
        raise _fail("result.timings.total_s", "does not match timestamps")
    if sum(float(timings[field]) for field in ("env_setup_s", "agent_s", "verifier_s")) > elapsed + 0.001:
        raise _fail("result.timings", "phase timings exceed total trial time")

    outcome = _object(result["outcome"], "result.outcome")
    _exact_fields(
        outcome,
        {"completed", "score", "checker_exit", "error", "failure_class", "failure_reason"},
        "result.outcome",
    )
    if not isinstance(outcome["completed"], bool):
        raise _fail("result.outcome.completed", "expected a boolean")
    if status == "completed":
        if not all(lock["environment"]["preflight"].values()):
            raise _fail("result.status", "completed trial has failed locked preflight")
        if not outcome["completed"] or outcome["error"] is not None:
            raise _fail("result.outcome", "completed status requires a non-error completed outcome")
        _number(outcome["score"], "result.outcome.score", maximum=1.0)
        _integer(outcome["checker_exit"], "result.outcome.checker_exit")
        expected_class = "solved" if outcome["checker_exit"] == 0 else "wrong_answer"
        if outcome["failure_class"] != expected_class or outcome["failure_reason"] is not None:
            raise _fail("result.outcome", "checker exit, score, and failure classification disagree")
        if (outcome["checker_exit"] == 0) != (float(outcome["score"]) == 1.0):
            raise _fail("result.outcome", "checker exit and score disagree")
    else:
        failed_preflight = not all(lock["environment"]["preflight"].values())
        if (status == "preflight_failed") != failed_preflight:
            raise _fail(
                "result.status",
                "preflight_failed status contradicts locked preflight evidence",
            )
        if status == "timeout" and elapsed + 0.001 < timeout_s:
            raise _fail("result.status", "timeout occurred before the locked deadline")
        if status == "retry_exhausted" and retries != lock["budget"]["max_retries"]:
            raise _fail(
                "result.retry_count",
                "retry_exhausted requires the locked retry budget to be exhausted",
            )
        if outcome["completed"] or outcome["score"] is not None or outcome["checker_exit"] is not None:
            raise _fail("result.outcome", "terminal status cannot contain a verifier verdict")
        _string(outcome["error"], "result.outcome.error")
        if outcome["failure_class"] != status:
            raise _fail("result.outcome.failure_class", "must equal terminal status")
        _optional_string(outcome["failure_reason"], "result.outcome.failure_reason")

    reward = _bound_json(
        root,
        "verifier/reward.json",
        trial_id=trial_id,
        lock_sha256=lock_sha256,
        fields={"status", "reward"},
    )
    evidence = _bound_json(
        root,
        "verifier/evidence.json",
        trial_id=trial_id,
        lock_sha256=lock_sha256,
        fields={"status", "checker_exit", "reward", "task_content_sha256", "final_state_sha256"},
    )
    if status == "completed":
        if reward != {
            **{key: reward[key] for key in ("schema_version", "trial_id", "lock_sha256")},
            "status": "judged",
            "reward": outcome["score"],
        }:
            raise _fail("verifier/reward.json", "does not match completed result")
        if (
            evidence["status"] != "judged"
            or evidence["checker_exit"] != outcome["checker_exit"]
            or evidence["reward"] != outcome["score"]
        ):
            raise _fail("verifier/evidence.json", "does not match completed result")
    else:
        if reward["status"] != "not_run" or reward["reward"] is not None:
            raise _fail("verifier/reward.json", "terminal result requires explicit not_run reward")
        if (
            evidence["status"] != "not_run"
            or evidence["checker_exit"] is not None
            or evidence["reward"] is not None
        ):
            raise _fail("verifier/evidence.json", "terminal result requires explicit not_run evidence")
    _digest(evidence["task_content_sha256"], "verifier/evidence.json.task_content_sha256")
    _digest(evidence["final_state_sha256"], "verifier/evidence.json.final_state_sha256")
    _scan_privacy(result, "result.json")
    _scan_privacy(reward, "verifier/reward.json")
    _scan_privacy(evidence, "verifier/evidence.json")
    _reject_harbor_shape(result, "result.json")
    return result, started, finished


def _usage_fields(usage: dict[str, int | None]) -> dict[str, Any]:
    if usage["input"] is None:
        return {
            "tokens": None,
            "tokens_input_uncached": None,
            "tokens_cache_read": None,
            "tokens_cache_write": None,
            "tokens_output": None,
            "tokens_reasoning": None,
            "usage_raw": None,
            "token_basis": "unmetered",
            "tokens_fresh": None,
        }
    uncached = usage["input"] - usage["cached"]
    return {
        "tokens": uncached + usage["output"],
        "tokens_input_uncached": uncached,
        "tokens_cache_read": usage["cached"],
        "tokens_cache_write": None,
        "tokens_output": usage["output"],
        "tokens_reasoning": None,
        "usage_raw": {
            "source": "native_atif",
            "input_tokens": usage["input"],
            "cached_tokens": usage["cached"],
            "output_tokens": usage["output"],
        },
        "token_basis": "native_atif",
        "tokens_fresh": uncached + usage["output"],
    }


def load_native_trial(bundle_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Validate one complete native macOS trial bundle and normalize one row."""
    requested = Path(bundle_dir).expanduser()
    if requested.is_symlink():
        raise _fail("bundle", "symlink directory is not accepted")
    root = requested.resolve()
    if not root.is_dir():
        raise _fail("bundle", f"not a directory: {root}")

    trial_id, inventory = _validate_manifest(root)
    lock, lock_sha256 = _validate_lock(root, trial_id)
    manifest = _read_json(root / MANIFEST_PATH, "manifest")
    if manifest["lock_sha256"] != lock_sha256:
        raise _fail("manifest.lock_sha256", "does not match validated lock")
    task_sidecar, native_sidecar = _validate_sidecars(
        root, lock, trial_id, lock_sha256
    )
    trajectory, usage, turns = _validate_trajectory(
        root, trial_id=trial_id, lock=lock
    )
    final_state_sha256 = _validate_artifacts(
        root,
        trial_id=trial_id,
        lock_sha256=lock_sha256,
        native_sidecar=native_sidecar,
    )
    result, started, finished = _validate_result_and_verifier(
        root,
        trial_id=trial_id,
        lock_sha256=lock_sha256,
        lock=lock,
    )
    verifier_evidence = _read_json(root / "verifier/evidence.json", "verifier/evidence.json")
    if verifier_evidence["task_content_sha256"] != task_sidecar["task_content_sha256"]:
        raise _fail("verifier/evidence.json.task_content_sha256", "does not match task sidecar")
    if verifier_evidence["final_state_sha256"] != final_state_sha256:
        raise _fail("verifier/evidence.json.final_state_sha256", "does not match artifacts")

    mcp_records, mcp_seal, mcp_verification = _verify_mcp_ledger(
        root,
        trial_id=trial_id,
        collector_run_id=lock["mcp"]["collector_run_id"],
        started=started,
        finished=finished,
    )
    focus_records = _verify_ledger(
        root,
        "focus",
        trial_id=trial_id,
        lock_sha256=lock_sha256,
        allowed_kinds={"focus_sample", "focus_yield"},
    )
    if result["mcp_event_count"] != len(mcp_records):
        raise _fail("result.mcp_event_count", "does not match MCP ledger")
    if result["focus_event_count"] != len(focus_records):
        raise _fail("result.focus_event_count", "does not match focus ledger")
    _integer(result["mcp_event_count"], "result.mcp_event_count")
    _integer(result["focus_event_count"], "result.focus_event_count")
    for prefix, records in (("focus", focus_records),):
        for index, record in enumerate(records):
            observed = _timestamp(
                record["timestamp"], f"{prefix}/ledger.jsonl:{index + 1}.timestamp"
            )
            if observed < started or observed > finished:
                raise _fail(
                    f"{prefix}/ledger.jsonl:{index + 1}.timestamp",
                    "falls outside trial timing",
                )
    target_bundle_id = lock["environment"]["app"]["bundle_id"]
    for index, record in enumerate(focus_records):
        payload = record["payload"]
        _exact_fields(
            payload,
            {"state", "frontmost_bundle_id", "target_bundle_id"},
            f"focus/ledger.jsonl:{index + 1}.payload",
        )
        if payload["state"] not in {"target_focused", "yielded_to_human"}:
            raise _fail(
                f"focus/ledger.jsonl:{index + 1}.payload.state",
                "focus violation is not importable",
            )
        if payload["target_bundle_id"] != target_bundle_id:
            raise _fail(
                f"focus/ledger.jsonl:{index + 1}.payload.target_bundle_id",
                "does not match locked app",
            )
        if (
            payload["state"] == "target_focused"
            and payload["frontmost_bundle_id"] != target_bundle_id
        ):
            raise _fail(
                f"focus/ledger.jsonl:{index + 1}.payload",
                "focus state contradicts frontmost app",
            )

    proxy_present = PROXY_FILES <= set(inventory)
    if lock["evidence"]["proxy_required"] != proxy_present:
        raise _fail(
            "lock.evidence.proxy_required",
            "required counting-proxy evidence presence does not match lock",
        )
    proxy_records: list[dict[str, Any]] = []
    proxy_totals: dict[str, int] | None = None
    if proxy_present:
        proxy_records = _verify_ledger(
            root,
            "proxy",
            trial_id=trial_id,
            lock_sha256=lock_sha256,
            allowed_kinds={"model_usage"},
        )
        proxy_totals = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
        for index, record in enumerate(proxy_records):
            payload = record["payload"]
            _exact_fields(
                payload,
                {"input_tokens", "cached_tokens", "output_tokens"},
                f"proxy/ledger.jsonl:{index + 1}.payload",
            )
            for field in proxy_totals:
                proxy_totals[field] += _integer(
                    payload[field],
                    f"proxy/ledger.jsonl:{index + 1}.payload.{field}",
                )
            if payload["cached_tokens"] > payload["input_tokens"]:
                raise _fail(
                    f"proxy/ledger.jsonl:{index + 1}.payload",
                    "cached tokens exceed input tokens",
                )
        if usage["input"] is None or proxy_totals != {
            "input_tokens": usage["input"],
            "cached_tokens": usage["cached"],
            "output_tokens": usage["output"],
        }:
            raise _fail("proxy/ledger.jsonl", "proxy totals do not reconcile with ATIF")

    outcome = result["outcome"]
    success = result["status"] == "completed" and outcome["checker_exit"] == 0
    trial_number = 1
    match = re.search(r"(?:^|[-_:])trial(\d+)$", trial_id)
    if match:
        trial_number = int(match.group(1))
        if trial_number < 1:
            raise _fail("manifest.trial_id", "trial suffix must be positive")
    row = {field: None for field in ROW_FIELDS}
    row.update(
        {
            "run_id": make_run_id(
                lock["harness"]["name"],
                lock["task"]["name"],
                lock["model"]["name"],
                trial_number,
                candidate_digest=lock_sha256,
                full_candidate_digest=True,
            ),
            "ts_iso": started.isoformat(),
            "harness": lock["harness"]["name"],
            "model": lock["model"]["name"],
            "task": lock["task"]["name"],
            "trial": trial_number,
            "success": success,
            "completed": outcome["completed"],
            "error": outcome["error"],
            "wall_time_s": round((finished - started).total_seconds(), 3),
            "t_env_setup_s": float(result["timings"]["env_setup_s"]),
            "t_agent_s": float(result["timings"]["agent_s"]),
            "t_checker_s": float(result["timings"]["verifier_s"]),
            "turns": turns,
            "checker_exit": outcome["checker_exit"],
            "exec_mode": "native_macos",
            "score": outcome["score"],
            "harness_version": lock["harness"]["version"],
            "harness_version_source": lock["harness"]["version_source"],
            "failure_class": outcome["failure_class"],
            "failure_reason": outcome["failure_reason"],
            "candidate_provenance": {
                "kind": "native_macos_trial",
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "trial_id": trial_id,
                "lock_sha256": lock_sha256,
                "result_sha256": _sha256_file(root / "result.json"),
                "manifest_sha256": _sha256_file(root / MANIFEST_PATH),
                "atif_sha256": _sha256_file(root / "agent/trajectory.json"),
                "reward_sha256": _sha256_file(root / "verifier/reward.json"),
                "verifier_evidence_sha256": _sha256_file(
                    root / "verifier/evidence.json"
                ),
                "artifact_manifest_sha256": _sha256_file(
                    root / "artifacts/manifest.json"
                ),
                "final_state_sha256": final_state_sha256,
                "mcp_ledger_sha256": _sha256_file(root / "mcp/ledger.jsonl"),
                "mcp_root_hash": mcp_verification.root_hash,
                "mcp_seal_hash": mcp_seal["seal_hash"],
                "focus_ledger_sha256": _sha256_file(root / "focus/ledger.jsonl"),
                "focus_seal_sha256": _sha256_file(root / "focus/seal.json"),
                "proxy_ledger_sha256": (
                    _sha256_file(root / "proxy/ledger.jsonl")
                    if proxy_present
                    else None
                ),
                "proxy_seal_sha256": (
                    _sha256_file(root / "proxy/seal.json")
                    if proxy_present
                    else None
                ),
                "task_sidecar_sha256": lock["task"]["sidecar_sha256"],
                "native_sidecar_sha256": lock["native_sidecar"]["sha256"],
                "task_content_sha256": task_sidecar["task_content_sha256"],
                "harness_identity": dict(lock["harness"]),
                "model_identity": dict(lock["model"]),
                "mcp_identity": dict(lock["mcp"]),
                "environment_identity": dict(lock["environment"]),
                "phase_timings": dict(result["timings"]),
                "retry_count": result["retry_count"],
                "max_retries": lock["budget"]["max_retries"],
                "terminal_status": (
                    result["status"] if result["status"] in TERMINAL_STATUSES else None
                ),
                "focus_event_count": len(focus_records),
                "mcp_event_count": len(mcp_records),
                "proxy_measured": proxy_present,
            },
            "version_drift": False,
            "timeout_s": float(lock["budget"]["timeout_s"]),
            "workspace_source": {
                "kind": "native_final_state",
                "sha256": final_state_sha256,
            },
            "usage_evidence_grade": (
                "proxy_reconciled" if proxy_present else "agent_reported"
            ),
            "usage_ranking_eligible": proxy_present,
            "usage_ranking_exclusion_reason": (
                None if proxy_present else "native_proxy_evidence_absent"
            ),
        }
    )
    row.update(_usage_fields(usage))
    if proxy_totals is not None:
        row.update(
            {
                "tokens_proxy_calls": len(proxy_records),
                "tokens_proxy_input_uncached": (
                    proxy_totals["input_tokens"] - proxy_totals["cached_tokens"]
                ),
                "tokens_proxy_cache_read": proxy_totals["cached_tokens"],
                "tokens_proxy_cache_write": None,
                "tokens_proxy_output": proxy_totals["output_tokens"],
                "tokens_proxy_reasoning": None,
                "token_basis_proxy": "native_counting_proxy",
                "proxy_capture_truncated": False,
            }
        )
    return row


def import_native_trial(
    bundle_dir: str | os.PathLike[str],
    results_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Append one validated native row, rejecting duplicate run identities."""
    row = load_native_trial(bundle_dir)
    requested = Path(results_path).expanduser()
    if requested.is_symlink():
        raise _fail("output", "symlink is not accepted")
    output = requested.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {field: row.get(field) for field in ROW_FIELDS},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with output.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        seen_run_ids: set[str] = set()
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise _fail("output", f"blank line at {line_number}")
            try:
                existing = json.loads(
                    line,
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_json_constant,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise _fail("output", f"invalid JSONL at line {line_number}: {exc}") from exc
            existing_run_id = existing.get("run_id") if isinstance(existing, dict) else None
            if existing_run_id is not None and existing_run_id in seen_run_ids:
                raise _fail("output", f"duplicate existing run_id {existing_run_id!r}")
            if existing_run_id is not None:
                seen_run_ids.add(existing_run_id)
            if existing_run_id == row["run_id"]:
                raise _fail("output", f"duplicate run_id {row['run_id']!r}")
        handle.seek(0, os.SEEK_END)
        append_offset = handle.tell()
        try:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.seek(append_offset)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            raise
    return row


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "LEDGER_SCHEMA_VERSION",
    "NATIVE_SIDECAR_SCHEMA_VERSION",
    "NativeTrialError",
    "TASK_SIDECAR_SCHEMA_VERSION",
    "import_native_trial",
    "load_native_trial",
]
