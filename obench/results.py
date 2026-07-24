"""Versioned result identity and fail-closed resume helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, TypeVar


LEGACY_SCHEMA_VERSION = 0
CURRENT_SCHEMA_VERSION = 2
HARNESS_BENCHMARK = "harness"
GATEWAY_BENCHMARK = "gateway"
_SHA256_HEX_LENGTH = 64

_IDENTITY_FIELDS = frozenset({
    "schema_version", "benchmark", "experiment", "arm", "comparison",
    "task", "harness", "execution", "schedule",
})
_BENCHMARK_FIELDS = frozenset({"name", "track"})
_EXPERIMENT_FIELDS = frozenset({"id", "digest"})
_ARM_FIELDS = frozenset({"id", "digest"})
_COMPARISON_FIELDS = frozenset({
    "policy_digest", "catalog_digest", "price_digest", "sampling_digest",
    "schedule_digest",
})
_TASK_FIELDS = frozenset({
    "name", "digest", "checker_digest", "workspace_source_sha",
})
_HARNESS_FIELDS = frozenset({"name", "candidate", "version"})
_EXECUTION_FIELDS = frozenset({
    "lane", "image_digest", "budget", "adapter_timeout_s",
    "checker_timeout_s",
})
_BUDGET_FIELDS = frozenset({
    "timeout_s", "max_calls", "max_output_tokens", "usd_cap",
})
_SCHEDULE_FIELDS = frozenset({
    "window_id", "repetition", "block_id", "block_attempt",
})


class ResultError(ValueError):
    """Base class for invalid result data."""


class ResultIdentityError(ResultError):
    """Raised when a result identity is incomplete or inconsistent."""


class UnsupportedResultError(ResultError):
    """Raised when no consumer is registered for a result kind."""


class ResultsLogError(RuntimeError):
    """Raised when a JSONL log cannot be resumed without ambiguity."""


class DuplicateResultError(ResultsLogError):
    """Raised when a JSONL log contains the same cell more than once."""


class _DuplicateJsonKey(ValueError):
    pass


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to deterministic, whitespace-free JSON."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return the UTF-8 bytes used for canonical result digests."""
    return canonical_json(value).encode("utf-8")


def canonical_digest(value: Any) -> str:
    """Return the lowercase SHA-256 hex digest of canonical JSON."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(f"duplicate object key {key!r}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _require_non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ResultIdentityError(f"{name} must be a non-empty string")
    return value


def _require_optional_string(name: str, value: Any) -> str | None:
    if value is not None:
        _require_non_empty_string(name, value)
    return value


def _require_digest(name: str, value: Any) -> str:
    value = _require_non_empty_string(name, value)
    if len(value) != _SHA256_HEX_LENGTH or any(c not in "0123456789abcdef" for c in value):
        raise ResultIdentityError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _require_optional_digest(name: str, value: Any) -> str | None:
    if value is not None:
        _require_digest(name, value)
    return value


def _require_source_sha(name: str, value: Any) -> str:
    value = _require_non_empty_string(name, value)
    if len(value) not in (40, 64) or any(c not in "0123456789abcdef" for c in value):
        raise ResultIdentityError(f"{name} must be a lowercase 40- or 64-character SHA")
    return value


def _require_positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ResultIdentityError(f"{name} must be a positive integer")
    return value


def _require_non_negative_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ResultIdentityError(f"{name} must be a non-negative integer")
    return value


def _require_positive_number(name: str, value: Any) -> int | float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
        or not math.isfinite(value)
    ):
        raise ResultIdentityError(f"{name} must be a positive finite number")
    return value


def _require_object(name: str, value: Any, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResultIdentityError(f"{name} must be an object")
    keys = frozenset(value)
    if not all(isinstance(key, str) for key in keys):
        raise ResultIdentityError(f"{name} field names must be strings")
    missing = sorted(fields - keys)
    extra = sorted(keys - fields)
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected fields: {', '.join(extra)}")
        raise ResultIdentityError(f"{name} has " + "; ".join(details))
    return value


@dataclass(frozen=True, slots=True)
class CellIdentity:
    """Immutable normative identity for one gateway benchmark cell."""

    schema_version: int
    benchmark: str
    track: str
    experiment_id: str
    experiment_digest: str
    arm_id: str
    arm_digest: str
    policy_digest: str
    catalog_digest: str
    price_digest: str
    sampling_digest: str
    schedule_digest: str
    task: str
    task_digest: str
    checker_digest: str
    workspace_source_sha: str
    harness: str
    candidate: str | None
    harness_version: str
    execution_lane: str
    image_digest: str | None
    budget_timeout_s: int | float
    budget_max_calls: int
    budget_max_output_tokens: int
    budget_usd_cap: str
    adapter_timeout_s: int | float
    checker_timeout_s: int | float
    window_id: str
    repetition: int
    block_id: str
    block_attempt: int

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ResultIdentityError(
                f"schema_version must be {CURRENT_SCHEMA_VERSION} for gateway identities"
            )
        if self.benchmark != GATEWAY_BENCHMARK:
            raise ResultIdentityError(f"benchmark must be {GATEWAY_BENCHMARK!r}")
        for name in (
            "track", "experiment_id", "arm_id", "task", "harness",
            "harness_version", "execution_lane", "budget_usd_cap", "window_id",
            "block_id",
        ):
            _require_non_empty_string(name, getattr(self, name))
        _require_optional_string("candidate", self.candidate)
        for name in (
            "experiment_digest", "arm_digest", "policy_digest", "catalog_digest",
            "price_digest", "sampling_digest", "schedule_digest", "task_digest",
            "checker_digest",
        ):
            _require_digest(name, getattr(self, name))
        _require_source_sha("workspace_source_sha", self.workspace_source_sha)
        _require_optional_digest("image_digest", self.image_digest)
        _require_positive_number("budget_timeout_s", self.budget_timeout_s)
        _require_positive_int("budget_max_calls", self.budget_max_calls)
        _require_positive_int("budget_max_output_tokens", self.budget_max_output_tokens)
        _require_positive_number("adapter_timeout_s", self.adapter_timeout_s)
        _require_positive_number("checker_timeout_s", self.checker_timeout_s)
        _require_positive_int("repetition", self.repetition)
        _require_non_negative_int("block_attempt", self.block_attempt)

    @classmethod
    def for_gateway(cls, **values: Any) -> "CellIdentity":
        """Build a schema-v2 gateway identity without repeating fixed fields."""
        return cls(
            schema_version=CURRENT_SCHEMA_VERSION,
            benchmark=GATEWAY_BENCHMARK,
            **values,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical nested identity representation."""
        return {
            "schema_version": self.schema_version,
            "benchmark": {"name": self.benchmark, "track": self.track},
            "experiment": {
                "id": self.experiment_id,
                "digest": self.experiment_digest,
            },
            "arm": {"id": self.arm_id, "digest": self.arm_digest},
            "comparison": {
                "policy_digest": self.policy_digest,
                "catalog_digest": self.catalog_digest,
                "price_digest": self.price_digest,
                "sampling_digest": self.sampling_digest,
                "schedule_digest": self.schedule_digest,
            },
            "task": {
                "name": self.task,
                "digest": self.task_digest,
                "checker_digest": self.checker_digest,
                "workspace_source_sha": self.workspace_source_sha,
            },
            "harness": {
                "name": self.harness,
                "candidate": self.candidate,
                "version": self.harness_version,
            },
            "execution": {
                "lane": self.execution_lane,
                "image_digest": self.image_digest,
                "budget": {
                    "timeout_s": self.budget_timeout_s,
                    "max_calls": self.budget_max_calls,
                    "max_output_tokens": self.budget_max_output_tokens,
                    "usd_cap": self.budget_usd_cap,
                },
                "adapter_timeout_s": self.adapter_timeout_s,
                "checker_timeout_s": self.checker_timeout_s,
            },
            "schedule": {
                "window_id": self.window_id,
                "repetition": self.repetition,
                "block_id": self.block_id,
                "block_attempt": self.block_attempt,
            },
        }

    def run_dict(self) -> dict[str, Any]:
        """Return comparison-shared dimensions that define one gateway run."""
        identity = self.as_dict()
        return {
            key: identity[key]
            for key in (
                "schema_version", "benchmark", "experiment", "comparison",
                "harness", "execution",
            )
        }


def validate_gateway_identity(value: CellIdentity | Mapping[str, Any]) -> CellIdentity:
    """Return a validated schema-v2 gateway identity."""
    if isinstance(value, CellIdentity):
        return value
    root = _require_object("gateway identity", value, _IDENTITY_FIELDS)
    benchmark = _require_object("identity.benchmark", root["benchmark"], _BENCHMARK_FIELDS)
    experiment = _require_object(
        "identity.experiment", root["experiment"], _EXPERIMENT_FIELDS
    )
    arm = _require_object("identity.arm", root["arm"], _ARM_FIELDS)
    comparison = _require_object(
        "identity.comparison", root["comparison"], _COMPARISON_FIELDS
    )
    task = _require_object("identity.task", root["task"], _TASK_FIELDS)
    harness = _require_object("identity.harness", root["harness"], _HARNESS_FIELDS)
    execution = _require_object(
        "identity.execution", root["execution"], _EXECUTION_FIELDS
    )
    budget = _require_object(
        "identity.execution.budget", execution["budget"], _BUDGET_FIELDS
    )
    schedule = _require_object("identity.schedule", root["schedule"], _SCHEDULE_FIELDS)
    return CellIdentity(
        schema_version=root["schema_version"],
        benchmark=benchmark["name"],
        track=benchmark["track"],
        experiment_id=experiment["id"],
        experiment_digest=experiment["digest"],
        arm_id=arm["id"],
        arm_digest=arm["digest"],
        policy_digest=comparison["policy_digest"],
        catalog_digest=comparison["catalog_digest"],
        price_digest=comparison["price_digest"],
        sampling_digest=comparison["sampling_digest"],
        schedule_digest=comparison["schedule_digest"],
        task=task["name"],
        task_digest=task["digest"],
        checker_digest=task["checker_digest"],
        workspace_source_sha=task["workspace_source_sha"],
        harness=harness["name"],
        candidate=harness["candidate"],
        harness_version=harness["version"],
        execution_lane=execution["lane"],
        image_digest=execution["image_digest"],
        budget_timeout_s=budget["timeout_s"],
        budget_max_calls=budget["max_calls"],
        budget_max_output_tokens=budget["max_output_tokens"],
        budget_usd_cap=budget["usd_cap"],
        adapter_timeout_s=execution["adapter_timeout_s"],
        checker_timeout_s=execution["checker_timeout_s"],
        window_id=schedule["window_id"],
        repetition=schedule["repetition"],
        block_id=schedule["block_id"],
        block_attempt=schedule["block_attempt"],
    )


def gateway_identity_from_row(row: Mapping[str, Any]) -> CellIdentity:
    """Read the canonical nested identity and verify dispatch metadata."""
    if "identity" not in row:
        raise ResultIdentityError("gateway row identity is required")
    identity = validate_gateway_identity(row["identity"])
    if row.get("schema_version") != identity.schema_version:
        raise ResultIdentityError("gateway row schema_version conflicts with its identity")
    if row.get("benchmark") != identity.benchmark:
        raise ResultIdentityError("gateway row benchmark conflicts with its identity")
    return identity


def make_gateway_run_id(identity: CellIdentity | Mapping[str, Any]) -> str:
    """Return the deterministic ID shared by comparable cells in one run."""
    identity = validate_gateway_identity(identity)
    return f"gateway-run-v{CURRENT_SCHEMA_VERSION}-{canonical_digest(identity.run_dict())}"


def make_gateway_cell_id(identity: CellIdentity | Mapping[str, Any]) -> str:
    """Return the deterministic ID for one gateway benchmark cell."""
    identity = validate_gateway_identity(identity)
    return f"gateway-cell-v{CURRENT_SCHEMA_VERSION}-{canonical_digest(identity.as_dict())}"


def schema_version(row: Mapping[str, Any]) -> int:
    """Return a row's schema version; missing means the legacy schema."""
    version = row.get("schema_version", LEGACY_SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise ResultError("schema_version must be a non-negative integer")
    return version


def benchmark_name(row: Mapping[str, Any]) -> str:
    """Return a row's benchmark, preserving metadata-free legacy dispatch."""
    version = schema_version(row)
    benchmark = row.get("benchmark")
    if version == LEGACY_SCHEMA_VERSION and benchmark is None:
        return HARNESS_BENCHMARK
    return _require_non_empty_string("benchmark", benchmark)


def result_kind(row: Mapping[str, Any]) -> tuple[int, str]:
    """Return the stable ``(schema_version, benchmark)`` dispatch key."""
    return schema_version(row), benchmark_name(row)


T = TypeVar("T")


def dispatch_result(
    row: Mapping[str, Any],
    handlers: Mapping[tuple[int, str], Callable[[Mapping[str, Any]], T]],
) -> T:
    """Dispatch a result row by schema version and benchmark."""
    kind = result_kind(row)
    try:
        handler = handlers[kind]
    except KeyError as exc:
        raise UnsupportedResultError(
            f"unsupported result schema_version={kind[0]} benchmark={kind[1]!r}"
        ) from exc
    return handler(row)


def result_cell_id(row: Mapping[str, Any]) -> str:
    """Return the resumable cell ID for a legacy or versioned result row."""
    if "identity" in row:
        identity = validate_gateway_identity(row["identity"])
        if row.get("schema_version") != identity.schema_version:
            raise ResultIdentityError(
                "gateway row schema_version conflicts with its identity"
            )
        if row.get("benchmark") != identity.benchmark:
            raise ResultIdentityError(
                "gateway row benchmark conflicts with its identity"
            )
    kind = result_kind(row)
    if kind == (LEGACY_SCHEMA_VERSION, HARNESS_BENCHMARK):
        value = row.get("run_id")
        if not isinstance(value, str) or not value:
            raise ResultIdentityError("legacy row run_id must be a non-empty string")
        return value
    if kind == (CURRENT_SCHEMA_VERSION, GATEWAY_BENCHMARK):
        identity = gateway_identity_from_row(row)
        if row.get("run_id") != make_gateway_run_id(identity):
            raise ResultIdentityError("gateway row run_id does not match its identity")
        if row.get("cell_id") != make_gateway_cell_id(identity):
            raise ResultIdentityError("gateway row cell_id does not match its identity")
        return row["cell_id"]
    raise UnsupportedResultError(
        f"unsupported result schema_version={kind[0]} benchmark={kind[1]!r}"
    )


def find_duplicate_cells(rows: Iterable[Mapping[str, Any]]) -> dict[str, tuple[int, ...]]:
    """Return duplicate cell IDs mapped to their one-based row numbers."""
    locations: dict[str, list[int]] = {}
    for line_no, row in enumerate(rows, 1):
        locations.setdefault(result_cell_id(row), []).append(line_no)
    return {
        cell_id: tuple(line_numbers)
        for cell_id, line_numbers in locations.items()
        if len(line_numbers) > 1
    }


@dataclass(frozen=True, slots=True)
class ResumeState:
    """Validated rows and cell IDs recovered from a results log."""

    rows: tuple[dict[str, Any], ...]
    cell_ids: frozenset[str]


def read_jsonl_for_resume(path: str | Path) -> ResumeState:
    """Read a results log, failing closed on corruption or duplicate cells."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return ResumeState((), frozenset())
    if not path.is_file():
        raise ResultsLogError(f"{path} exists but is not a regular file")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ResultsLogError(f"{path} has a truncated final JSONL line (missing newline)")

    rows: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(raw.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResultsLogError(f"{path} has invalid UTF-8 at line {line_no}") from exc
        try:
            row = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except ValueError as exc:
            message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
            raise ResultsLogError(
                f"{path} has corrupt JSON at line {line_no}: {message}"
            ) from exc
        if not isinstance(row, dict):
            raise ResultsLogError(f"{path} has a non-object JSON value at line {line_no}")
        try:
            result_cell_id(row)
        except ResultError as exc:
            raise ResultsLogError(
                f"{path} has invalid result identity at line {line_no}: {exc}"
            ) from exc
        rows.append(row)

    duplicates = find_duplicate_cells(rows)
    if duplicates:
        details = ", ".join(
            f"{cell_id} on lines {','.join(map(str, line_numbers))}"
            for cell_id, line_numbers in sorted(duplicates.items())
        )
        raise DuplicateResultError(f"{path} has duplicate result cells: {details}")
    return ResumeState(tuple(rows), frozenset(map(result_cell_id, rows)))


get_schema_version = schema_version
get_benchmark = benchmark_name
gateway_run_id = make_gateway_run_id
gateway_cell_id = make_gateway_cell_id
load_resume_state = read_jsonl_for_resume
