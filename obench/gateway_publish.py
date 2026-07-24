"""Create and verify sanitized, tamper-evident Gateway Bench bundles.

This module is deliberately a pure library.  Source artifacts are validated,
projected through explicit public DTO allowlists, and written to a new bundle.
The public ledger is re-chained after sanitization so verification never needs
the private source ledger.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from . import results
from . import gateway_spec


BUNDLE_SCHEMA_VERSION = 1
PROVENANCE_FILE = "provenance.json"
RESULTS_FILE = "results.jsonl"
SNAPSHOT_FILES = {
    "experiment": "experiment.json",
    "policy": "policy.json",
    "catalog": "catalog.json",
    "price": "prices.json",
}

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_HOME_PATH_RE = re.compile(r"(?:^|[\s\"'])/(?:Users|home)/[^/\s\"']+(?:/[^ \t\r\n\"']*)?")
_ABSOLUTE_PATH_RE = re.compile(r"(?:^|[\s\"'=])/(?!/)[^ \t\r\n\"']+")
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[^\\\r\n]+\\)*[^\\\r\n]*")
_SECRET_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b\d{12}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
        r"\s*[:=]\s*[\"']?[^,;\"'\s]{8,}"
    ),
)
_FORBIDDEN_KEY_PARTS = (
    "header", "query", "request_body", "response_body", "transcript",
    "endpoint", "account_id", "credential", "environment_value",
)
_FORBIDDEN_EXACT_KEYS = {
    "path", "absolute_path", "env", "environment", "authorization",
    "api_key", "apikey", "password", "secret",
}
_SAFE_PUBLIC_KEYS = {"allow_private_endpoint"}

_SCALAR = object()


class _NullableSchema:
    def __init__(self, schema: Any):
        self.schema = schema


_USAGE_SCHEMA = {
    "input_tokens": _SCALAR,
    "output_tokens": _SCALAR,
    "total_tokens": _SCALAR,
    "prompt_tokens": _SCALAR,
    "completion_tokens": _SCALAR,
    "cached_input_tokens": _SCALAR,
    "cache_read_input_tokens": _SCALAR,
    "cache_creation_input_tokens": _SCALAR,
    "reasoning_output_tokens": _SCALAR,
    "cost_usd": _SCALAR,
}
_TIMING_SCHEMA = {
    "ttfb_s": _SCALAR,
    "semantic_ttft_s": _SCALAR,
    "total_s": _SCALAR,
    "wall_time_s": _SCALAR,
}
_GENERATION_SCHEMA = {
    "output_tokens": _SCALAR,
    "duration_s": _SCALAR,
    "tokens_per_second": _SCALAR,
}
_ATTEMPT_SCHEMA = {
    "provider": _SCALAR,
    "model": _SCALAR,
    "status": _SCALAR,
}
_GATEWAY_METADATA_SCHEMA = {
    "generation_id_sha256": _SCALAR,
    "cost": _SCALAR,
    "market_cost": _SCALAR,
}
_ROUTE_SCHEMA = {
    "requested_model": _SCALAR,
    "metadata_requested_model": _SCALAR,
    "served_model": _SCALAR,
    "provider": _SCALAR,
    "attempts": [_ATTEMPT_SCHEMA],
    "gateway_metadata": _GATEWAY_METADATA_SCHEMA,
}
_ROUTE_EVIDENCE_SCHEMA = {
    "pass": _SCALAR,
    "verdict": _SCALAR,
    "reasons": [_SCALAR],
}
_COVERAGE_SCHEMA = {
    "covered": _SCALAR,
    "total": _SCALAR,
    "usage": _SCALAR,
    "semantic_ttft": _SCALAR,
    "route": _SCALAR,
    "attempts": _SCALAR,
    "attempt_evidence": _SCALAR,
}
_STREAM_SCHEMA = {
    "done": _SCALAR,
    "terminal_status": _SCALAR,
    "malformed_events": _SCALAR,
    "ignored_events": _SCALAR,
}
_METRICS_SCHEMA = {
    "timing": _NullableSchema(_TIMING_SCHEMA),
    "usage": _NullableSchema(_USAGE_SCHEMA),
    "generation": _NullableSchema(_GENERATION_SCHEMA),
    "route": _NullableSchema(_ROUTE_SCHEMA),
    "route_evidence": _NullableSchema(_ROUTE_EVIDENCE_SCHEMA),
    "coverage": _NullableSchema(_COVERAGE_SCHEMA),
    "stream": _NullableSchema(_STREAM_SCHEMA),
}
_COST_ITEM_SCHEMA = {
    "amount_usd": _SCALAR,
    "currency": _SCALAR,
    "effective_at": _SCALAR,
}
_COSTS_SCHEMA = {
    "gateway_reported": _COST_ITEM_SCHEMA,
    "invoice_reconciled": _COST_ITEM_SCHEMA,
    "frozen_list_estimate": _COST_ITEM_SCHEMA,
}
_REPORT_TIMING_SCHEMA = {
    "ttfb_s": _SCALAR,
    "semantic_ttft_s": _SCALAR,
}
_REPORT_GENERATION_SCHEMA = {
    "output_tokens": _SCALAR,
    "duration_s": _SCALAR,
}
_REPORT_CACHE_SCHEMA = {
    "cached_input_tokens": _SCALAR,
    "cache_write_input_tokens": _SCALAR,
}
_REPORT_ROUTE_SCHEMA = {
    "served_model": _SCALAR,
    "provider": _SCALAR,
    "attempts": [_ATTEMPT_SCHEMA],
    "attempts_present": _SCALAR,
}
_PROXY_CALL_SCHEMA = {
    "timing": _NullableSchema(_REPORT_TIMING_SCHEMA),
    "generation": _NullableSchema(_REPORT_GENERATION_SCHEMA),
    "route": _NullableSchema(_REPORT_ROUTE_SCHEMA),
    "cache": _NullableSchema(_REPORT_CACHE_SCHEMA),
    "costs": _NullableSchema(_COSTS_SCHEMA),
}
_CANONICAL_RESULT_SCHEMA = {
    "solved": _SCALAR,
    "checker_score": _SCALAR,
    "available": _SCALAR,
    "duration_s": _SCALAR,
    "timed_out": _SCALAR,
    "infrastructure_invalid_reason": _SCALAR,
    "infrastructure_valid": _SCALAR,
    "budget_exhausted_reason": _SCALAR,
}
_RESULT_SCHEMA = {
    "arm_role": _SCALAR,
    "baseline": _SCALAR,
    "model_match": _SCALAR,
    "result": _CANONICAL_RESULT_SCHEMA,
    "route_integrity": _ROUTE_EVIDENCE_SCHEMA,
    "proxy_metrics": {"calls": [_PROXY_CALL_SCHEMA]},
    "route_isolation": {
        "classification": _SCALAR,
        "lane": _SCALAR,
        "egress_enforced": _SCALAR,
    },
}
_SAMPLING_SCHEMA = {
    "model": _SCALAR,
    "temperature": _SCALAR,
    "top_p": _SCALAR,
    "top_k": _SCALAR,
    "max_tokens": _SCALAR,
    "max_completion_tokens": _SCALAR,
    "max_output_tokens": _SCALAR,
    "reasoning_effort": _SCALAR,
    "stream": _SCALAR,
    "seed": _SCALAR,
}
_SERVING_ARM_SCHEMA = {
    "arm_id": _SCALAR,
    "arm_digest": _SCALAR,
    "route_kind": _SCALAR,
}
_LEDGER_REQUEST_SCHEMA = {
    "ts": _SCALAR,
    "status": _SCALAR,
    "usage": _NullableSchema(_USAGE_SCHEMA),
    "model": _SCALAR,
    "sampling_observed": _SAMPLING_SCHEMA,
    "sampling_source": _SCALAR,
    "duration_ms": _SCALAR,
    "serving_arm": _SERVING_ARM_SCHEMA,
    "gateway_metrics": _NullableSchema(_METRICS_SCHEMA),
    "session_hash": _SCALAR,
    "previous_response_hash": _SCALAR,
    "response_hash": _SCALAR,
    "capture_truncated": _SCALAR,
}
_POLICY_SCHEMA = {
    "schema_version": _SCALAR,
    "id": _SCALAR,
    "policy_id": _SCALAR,
    "name": _SCALAR,
    "version": _SCALAR,
    "track": _SCALAR,
    "model_match": _SCALAR,
    "route_kind": _SCALAR,
    "allowed_models": [_SCALAR],
    "allowed_providers": [_SCALAR],
    "fallback_enabled": _SCALAR,
    "retry_count": _SCALAR,
    "cache_enabled": _SCALAR,
    "require_parameters": _SCALAR,
    "data_collection": _SCALAR,
}
_CATALOG_ENTRY_SCHEMA = {
    "model": _SCALAR,
    "model_id": _SCALAR,
    "provider": _SCALAR,
    "context_window": _SCALAR,
    "max_output_tokens": _SCALAR,
    "capabilities": [_SCALAR],
    "available": _SCALAR,
    "region": _SCALAR,
}
_CATALOG_SCHEMA = {
    "schema_version": _SCALAR,
    "id": _SCALAR,
    "catalog_id": _SCALAR,
    "name": _SCALAR,
    "version": _SCALAR,
    "generated_at": _SCALAR,
    "models": [_CATALOG_ENTRY_SCHEMA],
    "entries": [_CATALOG_ENTRY_SCHEMA],
}
_PRICE_ENTRY_SCHEMA = {
    "model": _SCALAR,
    "model_id": _SCALAR,
    "provider": _SCALAR,
    "input_per_million": _SCALAR,
    "output_per_million": _SCALAR,
    "cache_read_per_million": _SCALAR,
    "cache_write_per_million": _SCALAR,
    "input_cost_per_million": _SCALAR,
    "output_cost_per_million": _SCALAR,
    "currency": _SCALAR,
    "unit": _SCALAR,
    "effective_at": _SCALAR,
}
_PRICE_SCHEMA = {
    "schema_version": _SCALAR,
    "id": _SCALAR,
    "price_id": _SCALAR,
    "name": _SCALAR,
    "version": _SCALAR,
    "currency": _SCALAR,
    "effective_at": _SCALAR,
    "prices": [_PRICE_ENTRY_SCHEMA],
    "entries": [_PRICE_ENTRY_SCHEMA],
}
_WINDOW_SCHEMA = {
    "window_id": _SCALAR,
    "start": _SCALAR,
    "end": _SCALAR,
}
_BUDGET_SCHEMA = {
    "timeout_s": _SCALAR,
    "max_calls": _SCALAR,
    "max_output_tokens": _SCALAR,
    "usd_cap": _SCALAR,
}
_SAMPLING_PUBLIC_SCHEMA = {
    "temperature": _SCALAR,
    "top_p": _SCALAR,
    "seed": _SCALAR,
}
_EXPERIMENT_ARM_SCHEMA = {
    "arm_id": _SCALAR,
    "arm_digest": _SCALAR,
    "route_kind": _SCALAR,
    "gateway": _SCALAR,
    "protocol": _SCALAR,
    "baseline": _SCALAR,
    "canonical_model": _SCALAR,
    "requested_model": _SCALAR,
    "requested_provider": _SCALAR,
    "allowed_models": [_SCALAR],
    "allowed_providers": [_SCALAR],
    "fallback_enabled": _SCALAR,
    "retry_count": _SCALAR,
    "cache_enabled": _SCALAR,
    "sampling": _SAMPLING_PUBLIC_SCHEMA,
    "direct_control_arm_id": _SCALAR,
}
_EXPERIMENT_PUBLIC_SCHEMA = {
    "kind": _SCALAR,
    "source_digest": _SCALAR,
    "schema_version": _SCALAR,
    "experiment_id": _SCALAR,
    "track": _SCALAR,
    "model_match": _SCALAR,
    "harness": _SCALAR,
    "tasks": [_SCALAR],
    "repetitions_per_window": _SCALAR,
    "schedule_seed": _SCALAR,
    "execution_lane": _SCALAR,
    "allow_private_endpoint": _SCALAR,
    "windows": [_WINDOW_SCHEMA],
    "budget": _BUDGET_SCHEMA,
    "arms": [_EXPERIMENT_ARM_SCHEMA],
}


class GatewayPublishError(ValueError):
    """A Gateway Bench bundle is unsafe, incomplete, or inconsistent."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    try:
        return results.canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise GatewayPublishError(f"value is not canonical JSON: {exc}") from exc


def _artifact_bytes(value: Any) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise GatewayPublishError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise GatewayPublishError(f"non-standard JSON constant {value!r}")


def _decode_json(raw: bytes, source: str) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GatewayPublishError(f"{source} is not valid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, GatewayPublishError) as exc:
        raise GatewayPublishError(f"{source} is corrupt JSON: {exc}") from exc


def _read_json(path: str | os.PathLike[str]) -> Any:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise GatewayPublishError(f"{source} must be a regular, non-symlink file")
    return _decode_json(source.read_bytes(), str(source))


def _json_scalar(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise GatewayPublishError(f"{path} must be a finite JSON scalar")


def _project(value: Any, schema: Any, path: str) -> Any:
    if schema is _SCALAR:
        return _json_scalar(value, path)
    if isinstance(schema, _NullableSchema):
        if value is None:
            return None
        return _project(value, schema.schema, path)
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise GatewayPublishError(f"{path} must be an array")
        return [_project(item, schema[0], f"{path}[{index}]")
                for index, item in enumerate(value)]
    if not isinstance(value, Mapping):
        raise GatewayPublishError(f"{path} must be an object")
    return {
        key: _project(value[key], child_schema, f"{path}.{key}")
        for key, child_schema in schema.items()
        if key in value
    }


def _require_projected(value: Any, schema: Any, path: str) -> None:
    if _project(value, schema, path) != value:
        raise GatewayPublishError(f"{path} contains missing or extra public DTO fields")


def _require_public_result_shape(value: Mapping[str, Any], path: str) -> None:
    _require_projected(value, _RESULT_SCHEMA, path)
    required = {
        "arm_role", "baseline", "result", "route_integrity", "proxy_metrics",
        "route_isolation",
    }
    missing = sorted(required - set(value))
    if missing:
        raise GatewayPublishError(f"{path} is missing required fields: {missing!r}")

    required_result = {"solved", "checker_score", "available"}
    missing = sorted(required_result - set(value["result"]))
    if missing:
        raise GatewayPublishError(
            f"{path}.result is missing required fields: {missing!r}"
        )
    if set(value["route_integrity"]) != {"pass", "reasons"}:
        raise GatewayPublishError(
            f"{path}.route_integrity must contain pass and reasons"
        )
    if set(value["proxy_metrics"]) != {"calls"}:
        raise GatewayPublishError(f"{path}.proxy_metrics must contain calls")


def _assert_safe(value: Any, artifact: str) -> None:
    def walk(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                low = str(key).lower()
                if (
                    low not in _SAFE_PUBLIC_KEYS
                    and (
                        low in _FORBIDDEN_EXACT_KEYS
                        or any(part in low for part in _FORBIDDEN_KEY_PARTS)
                    )
                ):
                    raise GatewayPublishError(f"{artifact} contains forbidden field {path}.{key}")
                walk(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")
        elif isinstance(item, str):
            if _EMAIL_RE.search(item):
                raise GatewayPublishError(f"{artifact} contains an email address at {path}")
            if (
                _HOME_PATH_RE.search(item)
                or _ABSOLUTE_PATH_RE.search(item)
                or _WINDOWS_PATH_RE.search(item)
            ):
                raise GatewayPublishError(f"{artifact} contains a local absolute path at {path}")
            if re.search(r"(?i)\bhttps?://", item):
                raise GatewayPublishError(f"{artifact} contains an endpoint URL at {path}")
            if any(pattern.search(item) for pattern in _SECRET_PATTERNS):
                raise GatewayPublishError(f"{artifact} contains a credential pattern at {path}")

    walk(value, "$")


def _snapshot_source(value: Any, kind: str) -> tuple[Any, str]:
    if kind == "experiment" and isinstance(value, gateway_spec.GatewayExperiment):
        experiment = value
        return experiment.to_dict(), experiment.digest
    if isinstance(value, (str, os.PathLike)):
        path = Path(value)
        if kind == "experiment" and path.suffix.lower() == ".toml":
            try:
                experiment = gateway_spec.load_experiment(path)
            except (OSError, gateway_spec.GatewaySpecError) as exc:
                raise GatewayPublishError(f"invalid experiment {path}: {exc}") from exc
            return experiment.to_dict(), experiment.digest
        decoded = _read_json(path)
    else:
        decoded = value
    if not isinstance(decoded, Mapping):
        raise GatewayPublishError(f"{kind} snapshot must be a JSON object")
    if kind == "experiment":
        try:
            experiment = gateway_spec.parse_experiment(decoded)
        except gateway_spec.GatewaySpecError as exc:
            raise GatewayPublishError(f"invalid experiment snapshot: {exc}") from exc
        return experiment.to_dict(), experiment.digest
    return dict(decoded), results.canonical_digest(decoded)


def _experiment_dto(source: Mapping[str, Any], source_digest: str) -> dict[str, Any]:
    arms = []
    for arm in source["arms"]:
        public_arm = {
            "arm_id": arm["arm_id"],
            "arm_digest": gateway_spec.canonical_digest(arm),
            "route_kind": arm["route_kind"],
            "protocol": arm["protocol"],
            "baseline": arm["baseline"],
            "requested_model": arm["requested_model"],
            "requested_provider": arm["requested_provider"],
            "allowed_models": list(arm["allowed_models"]),
            "allowed_providers": list(arm["allowed_providers"]),
            "fallback_enabled": arm["fallback_enabled"],
            "retry_count": arm["retry_count"],
            "cache_enabled": arm["cache_enabled"],
            "sampling": dict(arm["sampling"]),
            "direct_control_arm_id": arm["direct_control_arm_id"],
        }
        if "gateway" in arm:
            public_arm["gateway"] = arm["gateway"]
        arms.append(public_arm)
    return {
        "kind": "experiment",
        "source_digest": source_digest,
        "schema_version": source["schema_version"],
        "experiment_id": source["experiment_id"],
        "track": source["track"],
        "model_match": source["model_match"],
        "harness": source["harness"],
        "tasks": list(source["tasks"]),
        "repetitions_per_window": source["repetitions_per_window"],
        "schedule_seed": source["schedule_seed"],
        "execution_lane": source["execution_lane"],
        "allow_private_endpoint": source["allow_private_endpoint"],
        "windows": [dict(window) for window in source["windows"]],
        "budget": dict(source["budget"]),
        "arms": arms,
    }


def _snapshot_dto(kind: str, source: Mapping[str, Any], digest: str) -> dict[str, Any]:
    schemas = {
        "policy": _POLICY_SCHEMA,
        "catalog": _CATALOG_SCHEMA,
        "price": _PRICE_SCHEMA,
    }
    return {
        "kind": kind,
        "source_digest": digest,
        "data": _project(source, schemas[kind], kind),
    }


def _load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise GatewayPublishError(f"{label} must be a regular, non-symlink file")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise GatewayPublishError(f"{label} must be non-empty JSONL ending in a newline")
    rows = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            raise GatewayPublishError(f"{label} has a blank line at {line_no}")
        row = _decode_json(line, f"{label} line {line_no}")
        if not isinstance(row, dict):
            raise GatewayPublishError(f"{label} line {line_no} is not an object")
        rows.append(row)
    return rows


def _validate_source_ledger(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = _load_jsonl(path, f"ledger {path}")
    seals = [index for index, row in enumerate(rows)
             if row.get("record_type") == "ledger_seal"]
    if seals != [len(rows) - 1]:
        raise GatewayPublishError(f"ledger {path} must have exactly one terminal seal")
    requests = rows[:-1]
    seal = rows[-1]
    previous = hashlib.sha256(b"").hexdigest()
    for sequence, row in enumerate(requests, 1):
        if row.get("record_type") != "request" or row.get("sequence") != sequence:
            raise GatewayPublishError(f"ledger {path} has invalid request sequence {sequence}")
        if row.get("previous_hash") != previous:
            raise GatewayPublishError(f"ledger {path} has a broken hash chain")
        unhashed = {key: value for key, value in row.items() if key != "record_hash"}
        expected = _sha256(_canonical_bytes(unhashed))
        if row.get("record_hash") != expected:
            raise GatewayPublishError(f"ledger {path} has a tampered request record")
        previous = expected
    expected_seal = {
        "record_type": "ledger_seal",
        "state": "SEALED",
        "record_count": len(requests),
        "last_sequence": len(requests),
        "root_hash": previous,
    }
    if seal != expected_seal:
        raise GatewayPublishError(f"ledger {path} has an invalid terminal seal")
    return requests, seal


def _public_ledger(
    cell_id: str,
    requests: list[dict[str, Any]],
) -> tuple[bytes, dict[str, Any]]:
    public_rows = []
    previous = hashlib.sha256(b"").hexdigest()
    for sequence, source in enumerate(requests, 1):
        projected = _project(source, _LEDGER_REQUEST_SCHEMA, f"ledger[{sequence}]")
        source_metrics = source.get("gateway_metrics")
        public_metrics = projected.get("gateway_metrics")
        if isinstance(source_metrics, Mapping) and isinstance(public_metrics, dict):
            source_route = source_metrics.get("route")
            public_route = public_metrics.get("route")
            if isinstance(source_route, Mapping) and isinstance(public_route, dict):
                metadata = source_route.get("gateway_metadata")
                if metadata is not None:
                    public_route["gateway_metadata"] = _gateway_metadata_dto(
                        metadata,
                        f"ledger[{sequence}].gateway_metrics.route.gateway_metadata",
                    )
        row = {
            "record_type": "request",
            "sequence": sequence,
            "previous_hash": previous,
            **projected,
        }
        row["record_hash"] = _sha256(_canonical_bytes(row))
        previous = row["record_hash"]
        public_rows.append(row)
    seal_body = {
        "record_type": "ledger_seal",
        "state": "SEALED",
        "cell_id": cell_id,
        "record_count": len(requests),
        "last_sequence": len(requests),
        "root_hash": previous,
    }
    seal = dict(seal_body, seal_sha256=_sha256(_canonical_bytes(seal_body)))
    public_rows.append(seal)
    _assert_safe(public_rows, f"ledger for {cell_id}")
    raw = b"".join(_artifact_bytes(row) for row in public_rows)
    return raw, seal


def _gateway_metadata_dto(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GatewayPublishError(f"{path} must be an object")
    result = {}
    opaque_ids = {"generationId": "generation_id_sha256"}
    for source_key, public_key in opaque_ids.items():
        if source_key not in value:
            continue
        raw = value[source_key]
        if not isinstance(raw, str) or not raw:
            raise GatewayPublishError(f"{path}.{source_key} must be a non-empty string")
        result[public_key] = _sha256(raw.encode("utf-8"))
    scalar_fields = {
        "cost": "cost",
        "marketCost": "market_cost",
    }
    for source_key, public_key in scalar_fields.items():
        if source_key in value:
            result[public_key] = _json_scalar(
                value[source_key],
                f"{path}.{source_key}",
            )
    return result


def _ledger_mapping(
    ledgers: Mapping[str, str | os.PathLike[str]] | str | os.PathLike[str],
) -> dict[str, Path]:
    if isinstance(ledgers, Mapping):
        mapping = {str(cell_id): Path(path) for cell_id, path in ledgers.items()}
    else:
        directory = Path(ledgers)
        if directory.is_symlink() or not directory.is_dir():
            raise GatewayPublishError("ledgers must be a mapping or a non-symlink directory")
        mapping = {path.stem: path for path in directory.iterdir()
                   if path.is_file() and path.suffix == ".jsonl"}
    if len(mapping) != len(set(mapping)):
        raise GatewayPublishError("duplicate ledger cell binding")
    return mapping


def _result_dto(
    row: Mapping[str, Any],
    ledger_binding: Mapping[str, Any],
) -> dict[str, Any]:
    identity = results.gateway_identity_from_row(row)
    public_result = _project(row, _RESULT_SCHEMA, "result")
    for call in public_result.get("proxy_metrics", {}).get("calls", []):
        for field in ("timing", "route"):
            if call.get(field) is None:
                call.pop(field)
    dto = {
        "schema_version": results.CURRENT_SCHEMA_VERSION,
        "benchmark": results.GATEWAY_BENCHMARK,
        "identity": identity.as_dict(),
        "run_id": results.make_gateway_run_id(identity),
        "cell_id": results.make_gateway_cell_id(identity),
        **public_result,
        "ledger": dict(ledger_binding),
    }
    if row.get("run_id") != dto["run_id"] or row.get("cell_id") != dto["cell_id"]:
        raise GatewayPublishError(f"result {dto['cell_id']} has inconsistent IDs")
    public_fields = {key: dto[key] for key in _RESULT_SCHEMA if key in dto}
    _require_public_result_shape(public_fields, f"result {dto['cell_id']}")
    _assert_safe(dto, f"result {dto['cell_id']}")
    return dto


def _write_file(root: Path, relative: str, raw: bytes) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return _sha256(raw)


def publish_bundle(
    results_path: str | os.PathLike[str],
    bundle_dir: str | os.PathLike[str],
    *,
    experiment: Any,
    policy: Any,
    catalog: Any,
    prices: Any,
    ledgers: Mapping[str, str | os.PathLike[str]] | str | os.PathLike[str],
) -> dict[str, Any]:
    """Create a new sanitized bundle and return its provenance object.

    ``ledgers`` is either ``{cell_id: sealed_ledger_path}`` or a directory
    containing ``<cell_id>.jsonl`` files.  Existing destinations are refused.
    """
    destination = Path(bundle_dir)
    if destination.exists() or destination.is_symlink():
        raise GatewayPublishError(f"bundle destination already exists: {destination}")

    try:
        resume = results.read_jsonl_for_resume(results_path)
    except results.ResultsLogError as exc:
        raise GatewayPublishError(str(exc)) from exc
    if not resume.rows:
        raise GatewayPublishError("results JSONL is empty")
    for row in resume.rows:
        if results.result_kind(row) != (
            results.CURRENT_SCHEMA_VERSION,
            results.GATEWAY_BENCHMARK,
        ):
            raise GatewayPublishError("bundle accepts only schema-v2 gateway results")

    ledger_sources = _ledger_mapping(ledgers)
    cell_ids = set(resume.cell_ids)
    if set(ledger_sources) != cell_ids:
        missing = sorted(cell_ids - set(ledger_sources))
        extra = sorted(set(ledger_sources) - cell_ids)
        raise GatewayPublishError(
            f"cell/ledger bindings mismatch; missing={missing!r} extra={extra!r}"
        )

    experiment_source, experiment_digest = _snapshot_source(experiment, "experiment")
    policy_source, policy_digest = _snapshot_source(policy, "policy")
    catalog_source, catalog_digest = _snapshot_source(catalog, "catalog")
    price_source, price_digest = _snapshot_source(prices, "price")
    snapshot_digests = {
        "experiment": experiment_digest,
        "policy": policy_digest,
        "catalog": catalog_digest,
        "price": price_digest,
    }

    identities = [results.gateway_identity_from_row(row) for row in resume.rows]
    for identity in identities:
        expected = {
            "experiment": identity.experiment_digest,
            "policy": identity.policy_digest,
            "catalog": identity.catalog_digest,
            "price": identity.price_digest,
        }
        if expected != snapshot_digests:
            raise GatewayPublishError(
                f"result {results.make_gateway_cell_id(identity)} snapshot digests do not match"
            )

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    try:
        artifacts: dict[str, str] = {}
        ledger_provenance: dict[str, Any] = {}
        bindings: dict[str, dict[str, Any]] = {}
        rows_by_cell = {
            results.result_cell_id(row): row for row in resume.rows
        }
        for cell_id in sorted(cell_ids):
            source_path = ledger_sources[cell_id]
            requests, source_seal = _validate_source_ledger(source_path)
            source_row = rows_by_cell[cell_id]
            identity = results.gateway_identity_from_row(source_row)
            expected_seal = source_row.get("ledger_seal")
            if not isinstance(expected_seal, Mapping):
                raise GatewayPublishError(f"result {cell_id} is missing ledger_seal")
            for field in ("record_count", "last_sequence", "root_hash"):
                if expected_seal.get(field) != source_seal.get(field):
                    raise GatewayPublishError(
                        f"result {cell_id} ledger_seal.{field} does not match source ledger"
                    )
            if expected_seal.get("ledger_file") != source_path.name:
                raise GatewayPublishError(
                    f"result {cell_id} ledger file does not match source ledger"
                )
            for request in requests:
                arm = request.get("serving_arm")
                if (
                    not isinstance(arm, Mapping)
                    or arm.get("arm_id") != identity.arm_id
                    or arm.get("arm_digest") != identity.arm_digest
                ):
                    raise GatewayPublishError(
                        f"result {cell_id} arm identity does not match source ledger"
                    )
            raw, seal = _public_ledger(cell_id, requests)
            relative = f"ledgers/{cell_id}.jsonl"
            artifacts[relative] = _write_file(temporary, relative, raw)
            binding = {
                "artifact": relative,
                "root_hash": seal["root_hash"],
                "seal_sha256": seal["seal_sha256"],
                "record_count": seal["record_count"],
            }
            bindings[cell_id] = binding
            ledger_provenance[cell_id] = dict(binding)

        public_rows = [
            _result_dto(row, bindings[results.result_cell_id(row)])
            for row in resume.rows
        ]
        results_raw = b"".join(_artifact_bytes(row) for row in public_rows)
        artifacts[RESULTS_FILE] = _write_file(temporary, RESULTS_FILE, results_raw)

        snapshot_values = {
            "experiment": _experiment_dto(experiment_source, experiment_digest),
            "policy": _snapshot_dto("policy", policy_source, policy_digest),
            "catalog": _snapshot_dto("catalog", catalog_source, catalog_digest),
            "price": _snapshot_dto("price", price_source, price_digest),
        }
        for kind, dto in snapshot_values.items():
            _assert_safe(dto, f"{kind} snapshot")
            relative = SNAPSHOT_FILES[kind]
            artifacts[relative] = _write_file(temporary, relative, _artifact_bytes(dto))

        provenance = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "bundle_kind": "gateway_bench",
            "result_schema_version": results.CURRENT_SCHEMA_VERSION,
            "result_count": len(public_rows),
            "snapshot_digests": snapshot_digests,
            "artifacts": dict(sorted(artifacts.items())),
            "ledgers": dict(sorted(ledger_provenance.items())),
        }
        _assert_safe(provenance, "provenance")
        _write_file(temporary, PROVENANCE_FILE, _artifact_bytes(provenance))
        temporary.rename(destination)
        return provenance
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise GatewayPublishError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _verify_public_ledger(
    raw: bytes,
    artifact: str,
    expected_cell_id: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="gateway_bundle_verify_") as tmp:
        path = Path(tmp) / "ledger.jsonl"
        path.write_bytes(raw)
        rows = _load_jsonl(path, artifact)
    if rows[-1].get("record_type") != "ledger_seal":
        raise GatewayPublishError(f"{artifact} has no terminal seal")
    requests = rows[:-1]
    previous = hashlib.sha256(b"").hexdigest()
    for sequence, row in enumerate(requests, 1):
        expected_keys = {
            "record_type", "sequence", "previous_hash", "record_hash",
            *_LEDGER_REQUEST_SCHEMA,
        }
        if set(row) - expected_keys:
            raise GatewayPublishError(f"{artifact} request {sequence} has extra fields")
        public_fields = {
            key: value for key, value in row.items()
            if key not in {"record_type", "sequence", "previous_hash", "record_hash"}
        }
        _require_projected(public_fields, _LEDGER_REQUEST_SCHEMA, f"{artifact}[{sequence}]")
        if row.get("record_type") != "request" or row.get("sequence") != sequence:
            raise GatewayPublishError(f"{artifact} has invalid sequence {sequence}")
        if row.get("previous_hash") != previous:
            raise GatewayPublishError(f"{artifact} has a broken hash chain")
        unhashed = {key: value for key, value in row.items() if key != "record_hash"}
        expected_hash = _sha256(_canonical_bytes(unhashed))
        if row.get("record_hash") != expected_hash:
            raise GatewayPublishError(f"{artifact} request {sequence} is tampered")
        previous = expected_hash
    seal = rows[-1]
    expected_body = {
        "record_type": "ledger_seal",
        "state": "SEALED",
        "cell_id": expected_cell_id,
        "record_count": len(requests),
        "last_sequence": len(requests),
        "root_hash": previous,
    }
    expected_seal = dict(
        expected_body,
        seal_sha256=_sha256(_canonical_bytes(expected_body)),
    )
    if seal != expected_seal:
        raise GatewayPublishError(f"{artifact} has an invalid seal or cell binding")
    _assert_safe(rows, artifact)
    return seal


def verify_bundle(bundle_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify a complete bundle, returning provenance or raising on any failure."""
    root = Path(bundle_dir)
    if root.is_symlink() or not root.is_dir():
        raise GatewayPublishError(f"bundle is not a non-symlink directory: {root}")
    provenance = _read_json(root / PROVENANCE_FILE)
    if not isinstance(provenance, dict):
        raise GatewayPublishError("provenance must be an object")
    expected_provenance_keys = {
        "schema_version", "bundle_kind", "result_schema_version",
        "result_count", "snapshot_digests", "artifacts", "ledgers",
    }
    if set(provenance) != expected_provenance_keys:
        raise GatewayPublishError("provenance has missing or extra fields")
    if (
        provenance["schema_version"] != BUNDLE_SCHEMA_VERSION
        or provenance["bundle_kind"] != "gateway_bench"
        or provenance["result_schema_version"] != results.CURRENT_SCHEMA_VERSION
    ):
        raise GatewayPublishError("unsupported bundle schema")
    artifacts = provenance["artifacts"]
    if not isinstance(artifacts, dict) or not artifacts:
        raise GatewayPublishError("provenance artifacts must be a non-empty object")
    required = {RESULTS_FILE, *SNAPSHOT_FILES.values()}
    if not required.issubset(artifacts):
        raise GatewayPublishError("provenance is missing required artifacts")

    actual_files = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise GatewayPublishError(f"bundle contains symlink: {path}")
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
    expected_files = set(artifacts) | {PROVENANCE_FILE}
    if actual_files != expected_files:
        raise GatewayPublishError(
            f"bundle artifact set mismatch; missing={sorted(expected_files - actual_files)!r} "
            f"extra={sorted(actual_files - expected_files)!r}"
        )
    for relative, expected_digest in artifacts.items():
        _require_digest(expected_digest, f"artifacts[{relative!r}]")
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise GatewayPublishError(f"unsafe artifact path: {relative!r}")
        actual = _sha256((root / relative).read_bytes())
        if actual != expected_digest:
            raise GatewayPublishError(f"artifact digest mismatch: {relative}")

    snapshot_digests = provenance["snapshot_digests"]
    if not isinstance(snapshot_digests, dict) or set(snapshot_digests) != set(SNAPSHOT_FILES):
        raise GatewayPublishError("invalid snapshot digest provenance")
    for kind, relative in SNAPSHOT_FILES.items():
        expected = _require_digest(snapshot_digests[kind], f"{kind} source digest")
        snapshot = _read_json(root / relative)
        if not isinstance(snapshot, dict) or snapshot.get("source_digest") != expected:
            raise GatewayPublishError(f"{kind} snapshot digest binding mismatch")
        if kind == "experiment":
            _require_projected(snapshot, _EXPERIMENT_PUBLIC_SCHEMA, kind)
        else:
            expected_keys = {"kind", "source_digest", "data"}
            if set(snapshot) != expected_keys or snapshot.get("kind") != kind:
                raise GatewayPublishError(f"{kind} snapshot has an invalid public DTO")
            schemas = {
                "policy": _POLICY_SCHEMA,
                "catalog": _CATALOG_SCHEMA,
                "price": _PRICE_SCHEMA,
            }
            _require_projected(snapshot["data"], schemas[kind], f"{kind}.data")
        _assert_safe(snapshot, f"{kind} snapshot")

    result_rows = _load_jsonl(root / RESULTS_FILE, RESULTS_FILE)
    if provenance["result_count"] != len(result_rows):
        raise GatewayPublishError("result count does not match provenance")
    ledger_meta = provenance["ledgers"]
    if not isinstance(ledger_meta, dict):
        raise GatewayPublishError("ledger provenance must be an object")
    seen_cells = set()
    for row in result_rows:
        if set(row) - {
            "schema_version", "benchmark", "identity", "run_id", "cell_id",
            *_RESULT_SCHEMA, "ledger",
        }:
            raise GatewayPublishError("public result has extra fields")
        public_fields = {
            key: value for key, value in row.items()
            if key in _RESULT_SCHEMA
        }
        _require_public_result_shape(public_fields, "result")
        try:
            cell_id = results.result_cell_id(row)
        except results.ResultError as exc:
            raise GatewayPublishError(f"invalid public result identity: {exc}") from exc
        if cell_id in seen_cells:
            raise GatewayPublishError(f"duplicate public result cell: {cell_id}")
        seen_cells.add(cell_id)
        identity = results.gateway_identity_from_row(row)
        expected_snapshots = {
            "experiment": identity.experiment_digest,
            "policy": identity.policy_digest,
            "catalog": identity.catalog_digest,
            "price": identity.price_digest,
        }
        if expected_snapshots != snapshot_digests:
            raise GatewayPublishError(f"result {cell_id} snapshot binding mismatch")
        binding = row.get("ledger")
        meta = ledger_meta.get(cell_id)
        if not isinstance(binding, dict) or binding != meta:
            raise GatewayPublishError(f"result {cell_id} ledger binding mismatch")
        _assert_safe(row, f"result {cell_id}")
    if seen_cells != set(ledger_meta):
        raise GatewayPublishError("result and ledger cell sets do not match")

    for cell_id, binding in ledger_meta.items():
        if not isinstance(binding, dict) or set(binding) != {
            "artifact", "root_hash", "seal_sha256", "record_count",
        }:
            raise GatewayPublishError(f"invalid ledger provenance for {cell_id}")
        artifact = binding["artifact"]
        if not isinstance(artifact, str) or artifact not in artifacts:
            raise GatewayPublishError(f"missing ledger artifact for {cell_id}")
        seal = _verify_public_ledger(
            (root / artifact).read_bytes(),
            artifact,
            cell_id,
        )
        expected_binding = {
            "artifact": artifact,
            "root_hash": seal["root_hash"],
            "seal_sha256": seal["seal_sha256"],
            "record_count": seal["record_count"],
        }
        if binding != expected_binding:
            raise GatewayPublishError(f"ledger root/seal mismatch for {cell_id}")

    _assert_safe(provenance, "provenance")
    return provenance
