"""Publication-safe reporting for matched native Computer-Use trials."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from statistics import median
from typing import Any, Mapping, Sequence

from .mcp_stdio_collector import (
    MetricsMetadataError,
    _validated_metrics,
    verify_ledger,
)
from .native_matrix import canonical_sha256, validate_native_matrix
from .native_trial import BUNDLE_SCHEMA_VERSION, load_native_trial
from .run import ROW_FIELDS, make_run_id
from .stats import wilson_ci


REPORT_SCHEMA_VERSION = "openbench.native-report.v2"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SECRET_RE = re.compile(
    r"\b(?:(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|gh[opsur]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|hf_[A-Za-z0-9]{20,})\b"
)
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/|file:///)")
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "prompt",
        "prompts",
        "screenshot",
        "screenshots",
        "atif",
        "trajectory",
        "payload",
        "payloads",
        "arguments",
        "raw",
        "usage_raw",
        "checker_stdout",
        "checker_stderr",
    }
)
_TOKEN_FIELDS = (
    "tokens_input_uncached",
    "tokens_cache_read",
    "tokens_cache_write",
    "tokens_output",
    "tokens_reasoning",
)
_METRIC_FIELDS = {
    "reward": "score",
    **{field: field for field in _TOKEN_FIELDS},
    "turns": "turns",
    "wall_time_s": "wall_time_s",
    "agent_time_s": "t_agent_s",
    "verifier_time_s": "t_checker_s",
}
_ATTRIBUTION_FIELDS = (
    "model_api_time_ms",
    "paced_wait_ms",
    "mcp_time_ms",
    "mcp_queue_time_ms",
    "perception_time_ms",
    "response_bytes",
    "text_bytes",
    "screenshot_png_bytes",
    "provider_input_tokens",
    "provider_cached_tokens",
    "provider_output_tokens",
    "model_mcp_overlap_ms",
    "residual_agent_time_ms",
)
_PERCEPTION_PHASE_FIELDS = (
    "settle_ms",
    "screenshot_ms",
    "snapshot_ms",
    "verification_ms",
    "response_construction_ms",
    "other_ms",
)


class NativeReportError(ValueError):
    """Raised when native comparison evidence is unsafe or non-comparable."""


@dataclass(frozen=True)
class _Observation:
    row: Mapping[str, Any]
    mcp_calls: tuple[Mapping[str, Any], ...] | None
    bundle_sha256: str
    result_sha256: str
    row_sha256: str
    proxy_requests: tuple[Mapping[str, Any], ...] | None = None


def _number(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    ):
        return float(value)
    return None


def _is_perception_capture(metric: Mapping[str, Any]) -> bool:
    """Distinguish a real capture from the MCP's explicit no-state sentinel."""
    if metric.get("response_encoding") != "none":
        return True
    zero_fields = (
        "elements_returned",
        "elements_visited",
        "perception_ms",
        "screenshot_png_bytes",
        *_PERCEPTION_PHASE_FIELDS,
    )
    if (
        metric.get("tool") != "state_result"
        or metric.get("partial") is not False
        or any(_number(metric.get(field)) != 0 for field in zero_fields)
    ):
        raise NativeReportError("no-state perception sentinel is malformed")
    return False


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    """Nearest-rank percentile, preserving observed tail values."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _summary(values: Sequence[Any], expected_n: int) -> dict[str, Any]:
    measured = [value for value in (_number(value) for value in values) if value is not None]
    return {
        "n": len(measured),
        "missing_n": expected_n - len(measured),
        "median": median(measured) if measured else None,
        "p95": _percentile(measured, 0.95),
    }


def _total_summary(values: Sequence[Any], expected_n: int) -> dict[str, Any]:
    summary = _summary(values, expected_n)
    measured = [value for value in values if _number(value) is not None]
    return {"total": sum(measured), **summary}


def _rate_summary(values: Sequence[Any], expected_n: int) -> dict[str, Any]:
    measured = [value for value in values if isinstance(value, bool)]
    true_count = sum(measured)
    return {
        "n": len(measured),
        "missing_n": expected_n - len(measured),
        "true_count": true_count,
        "rate": true_count / len(measured) if measured else None,
    }


def _privacy_scan(
    value: Any,
    location: str = "public_report",
    *,
    reject_raw_keys: bool = True,
) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if reject_raw_keys and key_text.lower() in _FORBIDDEN_PUBLIC_KEYS:
                raise NativeReportError(f"{location}.{key_text}: forbidden public field")
            _privacy_scan(
                item,
                f"{location}.{key_text}",
                reject_raw_keys=reject_raw_keys,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _privacy_scan(
                item,
                f"{location}[{index}]",
                reject_raw_keys=reject_raw_keys,
            )
    elif isinstance(value, str):
        if _EMAIL_RE.search(value):
            raise NativeReportError(f"{location}: email address is not publishable")
        if _SECRET_RE.search(value):
            raise NativeReportError(f"{location}: secret-like value is not publishable")
        if _HOME_PATH_RE.search(value):
            raise NativeReportError(f"{location}: absolute home path is not publishable")


def assert_public_native_report(report: Mapping[str, Any]) -> None:
    """Reject sensitive or raw evidence fields from a proposed public report."""
    if not isinstance(report, Mapping):
        raise NativeReportError("public report must be an object")
    _privacy_scan(report)


def _strict_native_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping) or set(row) != set(ROW_FIELDS):
        raise NativeReportError("row is not an exact normalized OpenBench result row")
    normalized = dict(row)
    provenance = normalized.get("candidate_provenance")
    if (
        normalized.get("exec_mode") != "native_macos"
        or normalized.get("version_drift") is not False
        or not isinstance(provenance, Mapping)
        or provenance.get("kind") != "native_macos_trial"
        or provenance.get("schema_version") != BUNDLE_SCHEMA_VERSION
    ):
        raise NativeReportError("row is not a strict imported native macOS row")
    for field in (
        "lock_sha256",
        "result_sha256",
        "manifest_sha256",
        "task_content_sha256",
        "mcp_ledger_sha256",
        "terminal_evidence_root",
        "result_identity_sha256",
    ):
        if not isinstance(provenance.get(field), str) or _DIGEST_RE.fullmatch(
            provenance[field]
        ) is None:
            raise NativeReportError(f"native row has invalid {field}")
    for field in ("success", "completed"):
        if not isinstance(normalized.get(field), bool):
            raise NativeReportError(f"native row {field} must be boolean")
    for field in ("harness_identity", "model_identity", "mcp_identity"):
        if not isinstance(provenance.get(field), Mapping):
            raise NativeReportError(f"native row has invalid {field}")
    for field in ("retry_count", "focus_event_count", "mcp_event_count"):
        value = provenance.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise NativeReportError(f"native row has invalid {field}")
    trial = normalized.get("trial")
    trial_id = provenance.get("trial_id")
    if (
        isinstance(trial, bool)
        or not isinstance(trial, int)
        or trial < 1
        or not isinstance(trial_id, str)
        or re.search(rf"(?:^|[-_:])trial{trial}\Z", trial_id) is None
    ):
        raise NativeReportError("native row trial and trial_id disagree")
    expected_run_id = make_run_id(
        normalized.get("harness"),
        normalized.get("task"),
        normalized.get("model"),
        trial,
        candidate_digest=provenance["result_identity_sha256"],
        full_candidate_digest=True,
    )
    if normalized.get("run_id") != expected_run_id:
        raise NativeReportError("native row run_id is not bound to its lock and trial")

    timings = provenance.get("phase_timings")
    if not isinstance(timings, Mapping) or set(timings) != {
        "env_setup_s",
        "agent_s",
        "verifier_s",
        "total_s",
    }:
        raise NativeReportError("native row phase timings are invalid")
    timing_pairs = (
        ("t_env_setup_s", "env_setup_s", 0.0),
        ("t_agent_s", "agent_s", 0.0),
        ("t_checker_s", "verifier_s", 0.0),
        # Import permits total_s within 1 ms of elapsed, then rounds row wall
        # time to 3 decimals. Those tolerances can combine to 1.5 ms.
        ("wall_time_s", "total_s", 0.001501),
    )
    for row_field, timing_field, tolerance in timing_pairs:
        if (
            _number(normalized.get(row_field)) is None
            or _number(timings.get(timing_field)) is None
            or abs(float(normalized[row_field]) - float(timings[timing_field]))
            > tolerance
        ):
            raise NativeReportError(f"native row {row_field} disagrees with phase timings")
    if (
        sum(
            float(timings[field])
            for field in ("env_setup_s", "agent_s", "verifier_s")
        )
        > float(timings["total_s"]) + 0.001
    ):
        raise NativeReportError("native row phase timings exceed total trial time")

    completed = normalized["completed"]
    success = normalized["success"]
    score = normalized.get("score")
    checker_exit = normalized.get("checker_exit")
    terminal_status = provenance.get("terminal_status")
    if completed:
        if (
            normalized.get("error") is not None
            or terminal_status is not None
            or _number(score) is None
            or float(score) > 1.0
            or isinstance(checker_exit, bool)
            or not isinstance(checker_exit, int)
            or checker_exit < 0
            or success != (checker_exit == 0 and float(score) == 1.0)
            or normalized.get("failure_class")
            != ("solved" if success else "wrong_answer")
        ):
            raise NativeReportError("native completed verdict fields disagree")
    elif (
        success
        or score is not None
        or checker_exit is not None
        or not isinstance(normalized.get("error"), str)
        or not normalized["error"]
        or terminal_status != normalized.get("failure_class")
    ):
        raise NativeReportError("native terminal verdict fields disagree")

    usage = normalized.get("usage_raw")
    if normalized.get("token_basis") == "native_atif":
        if (
            not isinstance(usage, Mapping)
            or usage.get("source") != "native_atif"
            or any(
                isinstance(usage.get(field), bool)
                or not isinstance(usage.get(field), int)
                or usage[field] < 0
                for field in ("input_tokens", "cached_tokens", "output_tokens")
            )
            or usage["cached_tokens"] > usage["input_tokens"]
        ):
            raise NativeReportError("native ATIF token accounting is invalid")
        uncached = usage["input_tokens"] - usage["cached_tokens"]
        fresh = uncached + usage["output_tokens"]
        if (
            normalized.get("tokens_input_uncached") != uncached
            or normalized.get("tokens_cache_read") != usage["cached_tokens"]
            or normalized.get("tokens_output") != usage["output_tokens"]
            or normalized.get("tokens") != fresh
            or normalized.get("tokens_fresh") != fresh
            or normalized.get("tokens_cache_write") is not None
            or normalized.get("tokens_reasoning") is not None
        ):
            raise NativeReportError("native row token fields disagree with ATIF usage")
    elif normalized.get("token_basis") == "unmetered":
        if any(
            normalized.get(field) is not None
            for field in (
                "usage_raw",
                "tokens",
                "tokens_fresh",
                *_TOKEN_FIELDS,
            )
        ):
            raise NativeReportError("unmetered native row contains token measurements")
    else:
        raise NativeReportError("native row has unsupported token basis")
    _privacy_scan(normalized, "native_row", reject_raw_keys=False)
    return normalized


def _load_bundle(path: str | Path) -> _Observation:
    root = Path(path)
    row = _strict_native_row(load_native_trial(root))
    verification = verify_ledger(root / "mcp/ledger.jsonl")
    provenance = row["candidate_provenance"]
    if verification.root_hash != provenance["mcp_root_hash"]:
        raise NativeReportError("validated MCP ledger root does not match native row")
    records = [
        json.loads(line)
        for line in (root / "mcp/ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    calls = tuple(record for record in records if record.get("record_type") == "tool_call")
    if len(calls) != provenance["mcp_event_count"]:
        raise NativeReportError("validated MCP call count does not match native row")
    for index, call in enumerate(calls, 1):
        meta = call.get("computer_use_meta")
        metrics = meta.get("metrics") if isinstance(meta, Mapping) else None
        if metrics is None:
            continue
        try:
            _validated_metrics(metrics)
        except MetricsMetadataError as exc:
            raise NativeReportError(
                f"validated MCP call {index} has invalid metrics metadata"
            ) from exc
    proxy_requests: tuple[Mapping[str, Any], ...] | None = None
    proxy_path = root / "proxy/ledger.jsonl"
    if proxy_path.is_file():
        proxy_records = [
            json.loads(line)
            for line in proxy_path.read_text(encoding="utf-8").splitlines()
        ]
        proxy_requests = tuple(
            record["payload"]
            for record in proxy_records
            if record.get("kind") == "model_request"
        )
    return _Observation(
        row=row,
        mcp_calls=calls,
        bundle_sha256=provenance["manifest_sha256"],
        result_sha256=provenance["result_sha256"],
        row_sha256=canonical_sha256({field: row[field] for field in ROW_FIELDS}),
        proxy_requests=proxy_requests,
    )


def _load_observation(value: Mapping[str, Any] | str | Path) -> _Observation:
    if isinstance(value, (str, Path)):
        return _load_bundle(value)
    row = _strict_native_row(value)
    provenance = row["candidate_provenance"]
    return _Observation(
        row=row,
        mcp_calls=None,
        bundle_sha256=provenance["manifest_sha256"],
        result_sha256=provenance["result_sha256"],
        row_sha256=canonical_sha256({field: row[field] for field in ROW_FIELDS}),
    )


def _merge_observation(
    previous: _Observation,
    observation: _Observation,
    *,
    cell_id: str,
) -> _Observation:
    if (
        previous.result_sha256 != observation.result_sha256
        or previous.bundle_sha256 != observation.bundle_sha256
        or previous.row_sha256 != observation.row_sha256
    ):
        raise NativeReportError(f"planned cell {cell_id!r} has conflicting results")
    if (
        previous.mcp_calls is None
        and observation.mcp_calls is not None
        or previous.proxy_requests is None
        and observation.proxy_requests is not None
    ):
        return observation
    return previous


def _row_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    provenance = row["candidate_provenance"]
    return {
        "task": {
            "name": row["task"],
            "content_sha256": provenance["task_content_sha256"],
        },
        "harness": dict(provenance["harness_identity"]),
        "model": dict(provenance["model_identity"]),
    }


def _match_cell(
    plan: Mapping[str, Any], observation: _Observation
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = observation.row
    provenance = row["candidate_provenance"]
    if _row_identity(row) != plan["fixed_identity"]:
        raise NativeReportError("native row task/harness/model identity is non-comparable")
    matching_arms = [
        arm
        for arm in plan["arms"]
        if arm["config_identity"]["mcp"] == provenance["mcp_identity"]
    ]
    if len(matching_arms) != 1:
        raise NativeReportError("native row MCP identity does not match exactly one arm")
    arm = matching_arms[0]
    matching_cells = [
        cell
        for cell in plan["schedule"]
        if cell["arm_id"] == arm["id"]
        and cell["trial_id"] == provenance["trial_id"]
        and cell["config_sha256"] == arm["config_sha256"]
    ]
    if len(matching_cells) != 1:
        raise NativeReportError("native row trial identity is not an exact planned cell")
    return arm, matching_cells[0]


def _mcp_categories(
    observations: Sequence[_Observation],
) -> tuple[dict[str, Any], list[str]]:
    unavailable = [item for item in observations if item.mcp_calls is None]
    if unavailable:
        return (
            {
                "available": False,
                "missing_bundle_n": len(unavailable),
                "calls_total": None,
                "calls_per_tool": None,
                "latency_ms": None,
                "latency_ms_per_tool": None,
                "operation_metrics_calls": None,
                "perception_metrics_calls": None,
                "queue_latency_ms": None,
                "execution_latency_ms": None,
                "elapsed_ms": None,
                "context_bytes": None,
                "elements_visited": None,
                "elements_returned": None,
                "partial_rate": None,
                "diff_rate": None,
                "error_counts": None,
                "outcome_counts": None,
                "delivery_counts": None,
                "focus_counts": None,
            },
            ["mcp_breakdown_requires_validated_bundles"],
        )

    calls = [call for item in observations for call in (item.mcp_calls or ())]
    by_tool: Counter[str] = Counter()
    latencies: defaultdict[str, list[float]] = defaultdict(list)
    errors: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    deliveries: Counter[str] = Counter()
    focus: Counter[str] = Counter()
    operation_metrics: list[Mapping[str, Any]] = []
    perception_metrics: list[Mapping[str, Any]] = []
    for call in calls:
        tool = str(call["tool"])
        by_tool[tool] += 1
        duration = _number(call.get("duration_ms"))
        if duration is not None:
            latencies[tool].append(duration)
        meta = call.get("computer_use_meta")
        meta = meta if isinstance(meta, Mapping) else {}
        error = meta.get("error")
        if isinstance(error, Mapping) and error.get("code") is not None:
            errors[str(error["code"])] += 1
        if call.get("tool_is_error") is True:
            errors["tool_is_error"] += 1
        rpc_error = call.get("jsonrpc_error")
        if isinstance(rpc_error, Mapping) and rpc_error.get("present") is True:
            errors["jsonrpc_error"] += 1
        outcome = meta.get("outcome")
        if isinstance(outcome, Mapping):
            if outcome.get("classification") is not None:
                outcomes[str(outcome["classification"])] += 1
            if outcome.get("failure_domain") is not None:
                outcomes[f"failure_domain:{outcome['failure_domain']}"] += 1
        delivery = meta.get("delivery")
        if isinstance(delivery, Mapping):
            if delivery.get("delivery_tier") is not None:
                deliveries[f"tier:{delivery['delivery_tier']}"] += 1
            for reason in delivery.get("fallback_reasons", []):
                deliveries[f"fallback:{reason}"] += 1
            if delivery.get("chain_rung") is not None:
                deliveries[f"chain:{delivery['chain_rung']}"] += 1
        focus_meta = meta.get("focus")
        if isinstance(focus_meta, Mapping):
            for key, value in focus_meta.items():
                if isinstance(value, bool):
                    focus[f"{key}:{str(value).lower()}"] += 1
        metrics = meta.get("metrics")
        metrics = metrics if isinstance(metrics, Mapping) else {}
        operation = metrics.get("operation")
        if isinstance(operation, Mapping):
            operation_metrics.append(operation)
        perception = metrics.get("perception")
        if isinstance(perception, Mapping):
            perception_metrics.append(perception)
    all_latencies = [value for values in latencies.values() for value in values]
    response_bytes = [
        int(call["response_bytes"])
        for call in calls
        if isinstance(call.get("response_bytes"), int)
        and not isinstance(call["response_bytes"], bool)
        and call["response_bytes"] >= 0
    ]
    return (
        {
            "available": True,
            "missing_bundle_n": 0,
            "calls_total": len(calls),
            "calls_per_tool": dict(sorted(by_tool.items())),
            "latency_ms": {
                "n": len(all_latencies),
                "p50": median(all_latencies) if all_latencies else None,
                "p95": _percentile(all_latencies, 0.95),
            },
            "latency_ms_per_tool": {
                tool: {
                    "n": len(values),
                    "p50": median(values),
                    "p95": _percentile(values, 0.95),
                }
                for tool, values in sorted(latencies.items())
            },
            "operation_metrics_calls": len(operation_metrics),
            "perception_metrics_calls": len(perception_metrics),
            "queue_latency_ms": _summary(
                [metric.get("queue_latency_ms") for metric in operation_metrics],
                len(operation_metrics),
            ),
            "execution_latency_ms": _summary(
                [metric.get("execution_latency_ms") for metric in operation_metrics],
                len(operation_metrics),
            ),
            "elapsed_ms": _summary(
                [
                    metric.get("perception_ms", metric.get("elapsed_ms"))
                    for metric in perception_metrics
                ],
                len(perception_metrics),
            ),
            "context_bytes": _total_summary(
                [metric.get("context_bytes") for metric in perception_metrics],
                len(perception_metrics),
            ),
            "elements_visited": _total_summary(
                [metric.get("elements_visited") for metric in perception_metrics],
                len(perception_metrics),
            ),
            "elements_returned": _total_summary(
                [metric.get("elements_returned") for metric in perception_metrics],
                len(perception_metrics),
            ),
            "partial_rate": _rate_summary(
                [metric.get("partial") for metric in perception_metrics],
                len(perception_metrics),
            ),
            "diff_rate": _rate_summary(
                [metric.get("diff") for metric in perception_metrics],
                len(perception_metrics),
            ),
            "response_bytes": {
                "total": sum(response_bytes),
                **_summary(response_bytes, len(calls)),
            },
            "error_counts": dict(sorted(errors.items())),
            "outcome_counts": dict(sorted(outcomes.items())),
            "delivery_counts": dict(sorted(deliveries.items())),
            "focus_counts": dict(sorted(focus.items())),
        },
        [],
    )


def _model_categories(
    observations: Sequence[_Observation],
) -> tuple[dict[str, Any], list[str]]:
    unavailable = [
        item for item in observations if item.proxy_requests is None
    ]
    if unavailable:
        return (
            {
                "available": False,
                "missing_bundle_n": len(unavailable),
                "requests_total": None,
                "latency_ms": None,
                "status_counts": None,
                "error_requests": None,
                "usage_available_requests": None,
                "input_tokens_per_request": None,
                "cached_tokens_per_request": None,
                "output_tokens_per_request": None,
            },
            ["model_request_breakdown_requires_validated_proxy_bundles"],
        )
    requests = [
        request
        for item in observations
        for request in (item.proxy_requests or ())
    ]
    latencies = [request["duration_ms"] for request in requests]
    usage_requests = [
        request for request in requests if request["usage_available"]
    ]
    return (
        {
            "available": True,
            "missing_bundle_n": 0,
            "requests_total": len(requests),
            "latency_ms": {
                "n": len(latencies),
                "p50": median(latencies) if latencies else None,
                "p95": _percentile(latencies, 0.95),
                "total": sum(latencies),
            },
            "status_counts": dict(
                sorted(Counter(str(request["status"]) for request in requests).items())
            ),
            "error_requests": sum(
                bool(request["error_present"]) or request["status"] >= 400
                for request in requests
            ),
            "usage_available_requests": len(usage_requests),
            "input_tokens_per_request": _summary(
                [request["input_tokens"] for request in usage_requests],
                len(requests),
            ),
            "cached_tokens_per_request": _summary(
                [request["cached_tokens"] for request in usage_requests],
                len(requests),
            ),
            "output_tokens_per_request": _summary(
                [request["output_tokens"] for request in usage_requests],
                len(requests),
            ),
        },
        [],
    )


def _merge_intervals(
    intervals: Sequence[tuple[int, int, str]],
) -> tuple[list[tuple[int, int, str]], int]:
    merged: list[tuple[int, int, str]] = []
    overlap_ns = 0
    for start, end, label in sorted(intervals):
        if not merged or start >= merged[-1][1]:
            merged.append((start, end, label))
            continue
        prior_start, prior_end, prior_label = merged[-1]
        overlap_ns += max(0, min(prior_end, end) - start)
        merged[-1] = (prior_start, max(prior_end, end), f"{prior_label}+{label}")
    return merged, overlap_ns


def _trial_attribution(observation: _Observation) -> dict[str, Any] | None:
    if observation.mcp_calls is None or observation.proxy_requests is None:
        return None
    calls = observation.mcp_calls
    requests = observation.proxy_requests
    successful_usage = [
        request
        for request in requests
        if request.get("usage_available") is True
        and type(request.get("status")) is int
        and 200 <= request["status"] < 400
    ]
    intervals_by_kind: dict[str, list[tuple[int, int, str]]] = {}
    same_source_overlap_ns: dict[str, int] = {}
    for kind, records in (("model request", successful_usage), ("MCP call", calls)):
        intervals: list[tuple[int, int, str]] = []
        for index, record in enumerate(records, 1):
            start = record.get("request_unix_ns")
            end = record.get("response_unix_ns")
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end < start
            ):
                raise NativeReportError(f"{kind} interval is malformed")
            intervals.append((start, end, f"{kind} {index}"))
        merged, overlap_ns = _merge_intervals(intervals)
        intervals_by_kind[kind] = merged
        same_source_overlap_ns[kind] = overlap_ns

    model_intervals = intervals_by_kind["model request"]
    mcp_intervals = intervals_by_kind["MCP call"]
    overlap_total_ns = 0
    overlap_pair_count = 0
    max_pair_overlap_ns = 0
    model_index = 0
    mcp_index = 0
    while model_index < len(model_intervals) and mcp_index < len(mcp_intervals):
        model_start, model_end, _ = model_intervals[model_index]
        mcp_start, mcp_end, _ = mcp_intervals[mcp_index]
        overlap_ns = max(0, min(model_end, mcp_end) - max(model_start, mcp_start))
        if overlap_ns:
            overlap_total_ns += overlap_ns
            overlap_pair_count += 1
            max_pair_overlap_ns = max(max_pair_overlap_ns, overlap_ns)
        if model_end <= mcp_end:
            model_index += 1
        else:
            mcp_index += 1
    model_request_time_ms = 0.0
    model_api_intervals: list[tuple[int, int, str]] = []
    for index, request in enumerate(successful_usage, 1):
        duration = _number(request.get("duration_ms"))
        paced = _number(request.get("paced_wait_ms"))
        if duration is None or paced is None or paced > duration:
            raise NativeReportError("model request timing is malformed")
        model_request_time_ms += duration
        start = request["request_unix_ns"] + round(paced * 1_000_000)
        end = request["response_unix_ns"]
        if start > end:
            raise NativeReportError("model request timing is malformed")
        model_api_intervals.append((start, end, f"model API {index}"))
    model_same_source_overlap_ms = (
        same_source_overlap_ns["model request"] / 1_000_000
    )
    model_request_time_ms = max(
        0.0, model_request_time_ms - model_same_source_overlap_ms
    )
    merged_api_intervals, model_api_overlap_ns = _merge_intervals(
        model_api_intervals
    )
    model_api_time_ms = sum(
        end - start for start, end, _label in merged_api_intervals
    ) / 1_000_000
    paced_wait_values = [
        _number(request.get("paced_wait_ms")) for request in successful_usage
    ]
    if any(value is None for value in paced_wait_values):
        raise NativeReportError("model request paced wait is malformed")
    call_durations = [_number(call.get("duration_ms")) for call in calls]
    if any(value is None for value in call_durations):
        raise NativeReportError("MCP call duration is malformed")

    operation_metrics = []
    response_metrics = []
    perception_metrics = []
    for call in calls:
        meta = call.get("computer_use_meta")
        metrics = meta.get("metrics") if isinstance(meta, Mapping) else None
        if not isinstance(metrics, Mapping):
            continue
        if isinstance(metrics.get("operation"), Mapping):
            operation_metrics.append(metrics["operation"])
        if isinstance(metrics.get("perception"), Mapping):
            response_metric = metrics["perception"]
            response_metrics.append(response_metric)
            if _is_perception_capture(response_metric):
                perception_metrics.append(response_metric)
    mcp_identity = observation.row["candidate_provenance"]["mcp_identity"]
    call_contract = mcp_identity.get("call_contract")
    if call_contract:
        ordered_calls = sorted(
            calls, key=lambda item: item.get("contract_sequence", -1)
        )
        for index, (expected, call) in enumerate(
            zip(call_contract, ordered_calls), 1
        ):
            meta = call.get("computer_use_meta")
            metrics = meta.get("metrics") if isinstance(meta, Mapping) else None
            perception = metrics.get("perception") if isinstance(metrics, Mapping) else None
            has_perception = (
                isinstance(perception, Mapping)
                and _is_perception_capture(perception)
            )
            expects_perception = (
                expected.get("tool") == "get_app_state"
                or expected.get("required_arguments", {}).get("include_state") is True
            )
            if has_perception != expects_perception:
                status = "missing" if expects_perception else "unexpected"
                raise NativeReportError(
                    f"contracted MCP call {index} has {status} perception telemetry; "
                    "trial attribution is non-comparable"
                )
    queue_values = [_number(metric.get("queue_latency_ms")) for metric in operation_metrics]
    perception_values = [
        _number(metric.get("perception_ms", metric.get("elapsed_ms")))
        for metric in perception_metrics
    ]
    if any(value is None for value in (*queue_values, *perception_values)):
        raise NativeReportError("MCP component timing is malformed")
    phase_totals: Counter[str] = Counter()
    encodings: Counter[str] = Counter()
    for metric in perception_metrics:
        if "perception_ms" in metric:
            phases = {field: metric.get(field) for field in _PERCEPTION_PHASE_FIELDS}
            for name, value in phases.items():
                measured = _number(value)
                if measured is None:
                    raise NativeReportError("perception phase timing is malformed")
                phase_totals[name] += measured
        if metric.get("response_encoding") is not None:
            encodings[str(metric["response_encoding"])] += 1

    def total_metric(field: str) -> int | None:
        values = [_number(metric.get(field)) for metric in response_metrics]
        present = [value for value in values if value is not None]
        if present and len(present) != len(values):
            raise NativeReportError(f"perception {field} is only partially measured")
        return int(sum(present)) if present else None

    response_values = [_number(call.get("response_bytes")) for call in calls]
    if any(value is None for value in response_values):
        raise NativeReportError("MCP response wire size is malformed")
    paced_wait_ms = sum(value for value in paced_wait_values if value is not None)
    mcp_same_source_overlap_ms = same_source_overlap_ns["MCP call"] / 1_000_000
    mcp_time_ms = max(
        0.0,
        sum(value for value in call_durations if value is not None)
        - mcp_same_source_overlap_ms,
    )
    agent_time_ms = float(observation.row["t_agent_s"]) * 1000.0
    overlap_total_ms = overlap_total_ns / 1_000_000
    max_pair_overlap_ms = max_pair_overlap_ns / 1_000_000
    residual: float | None = None
    unavailable_reason: str | None = None
    if overlap_total_ns:
        unavailable_reason = "model_mcp_interval_overlap"
    else:
        residual = agent_time_ms - model_request_time_ms - mcp_time_ms
        if residual < -1.0:
            raise NativeReportError(
                "exclusive timing attribution exceeds agent time"
            )
    return {
        "trial": observation.row["trial"],
        "observed_time_ms": {
            "model_request_ms": model_request_time_ms,
            "model_api_ms": model_api_time_ms,
            "paced_wait_ms": paced_wait_ms,
            "mcp_call_ms": mcp_time_ms,
        },
        "model_mcp_overlap": {
            "total_ms": overlap_total_ms,
            "interval_pair_count": overlap_pair_count,
            "max_pair_ms": max_pair_overlap_ms,
        },
        "same_source_overlap": {
            "model_request_ms": model_same_source_overlap_ms,
            "model_api_ms": model_api_overlap_ns / 1_000_000,
            "mcp_call_ms": mcp_same_source_overlap_ms,
        },
        "exclusive_partition": {
            "available": residual is not None,
            "residual_agent_time_ms": (
                max(0.0, residual) if residual is not None else None
            ),
            "unavailable_reason": unavailable_reason,
        },
        "mcp_detail_ms": {
            "queue_time_ms": (
                sum(value for value in queue_values if value is not None)
                if queue_values
                else None
            ),
            "perception_time_ms": (
                sum(value for value in perception_values if value is not None)
                if perception_values
                else None
            ),
            "perception_phase_time_ms": dict(sorted(phase_totals.items())),
        },
        "bytes": {
            "response_bytes": int(sum(value for value in response_values if value is not None)),
            "text_bytes": total_metric("text_bytes"),
            "screenshot_png_bytes": total_metric("screenshot_png_bytes"),
        },
        "provider_tokens": {
            "input": sum(int(request["input_tokens"]) for request in successful_usage),
            "cached": sum(int(request["cached_tokens"]) for request in successful_usage),
            "output": sum(int(request["output_tokens"]) for request in successful_usage),
        },
        "response_encoding_counts": dict(sorted(encodings.items())),
    }


def _flat_attribution(value: Mapping[str, Any] | None) -> dict[str, float | None]:
    if value is None:
        return {field: None for field in _ATTRIBUTION_FIELDS}
    return {
        "model_api_time_ms": value["observed_time_ms"]["model_api_ms"],
        "paced_wait_ms": value["observed_time_ms"]["paced_wait_ms"],
        "mcp_time_ms": value["observed_time_ms"]["mcp_call_ms"],
        "mcp_queue_time_ms": value["mcp_detail_ms"]["queue_time_ms"],
        "perception_time_ms": value["mcp_detail_ms"]["perception_time_ms"],
        "response_bytes": value["bytes"]["response_bytes"],
        "text_bytes": value["bytes"]["text_bytes"],
        "screenshot_png_bytes": value["bytes"]["screenshot_png_bytes"],
        "provider_input_tokens": value["provider_tokens"]["input"],
        "provider_cached_tokens": value["provider_tokens"]["cached"],
        "provider_output_tokens": value["provider_tokens"]["output"],
        "model_mcp_overlap_ms": value["model_mcp_overlap"]["total_ms"],
        "residual_agent_time_ms": value["exclusive_partition"]["residual_agent_time_ms"],
    }


def _aggregate_observations(
    observations: Sequence[_Observation],
) -> dict[str, Any]:
    n = len(observations)
    rows = [item.row for item in observations]
    successes = sum(1 for row in rows if row["success"])
    low, high = wilson_ci(successes, n)
    continuous = {
        public_name: _summary([row.get(row_field) for row in rows], n)
        for public_name, row_field in _METRIC_FIELDS.items()
    }
    fresh_tokens = [
        (
            _number(row.get("tokens_input_uncached")) or 0.0
        )
        + (_number(row.get("tokens_output")) or 0.0)
        if _number(row.get("tokens_input_uncached")) is not None
        and _number(row.get("tokens_output")) is not None
        else None
        for row in rows
    ]
    context_bloat = []
    cache_share = []
    for row in rows:
        uncached = _number(row.get("tokens_input_uncached"))
        cached = _number(row.get("tokens_cache_read"))
        turns = _number(row.get("turns"))
        if uncached is None or cached is None or turns is None or turns == 0:
            context_bloat.append(None)
        else:
            context_bloat.append((uncached + cached) / turns)
        if uncached is None or cached is None or uncached + cached == 0:
            cache_share.append(None)
        else:
            cache_share.append(cached / (uncached + cached))

    mcp, missing = _mcp_categories(observations)
    model, model_missing = _model_categories(observations)
    trial_attribution = [_trial_attribution(item) for item in observations]
    flat_attribution = [_flat_attribution(value) for value in trial_attribution]
    phase_names = sorted({
        name
        for value in trial_attribution
        if value is not None
        for name in value["mcp_detail_ms"]["perception_phase_time_ms"]
    })
    total_fresh = (
        sum(value for value in fresh_tokens if value is not None)
        if all(value is not None for value in fresh_tokens)
        else None
    )
    total_turns = (
        sum(float(row["turns"]) for row in rows)
        if all(_number(row.get("turns")) is not None for row in rows)
        else None
    )
    total_actions = mcp["calls_total"] if mcp["available"] else None
    return {
        "n": n,
        "success": {
            "count": successes,
            "rate": successes / n if n else None,
            "wilson_95": [low, high] if n else [0.0, 1.0],
        },
        "metrics": {
            **continuous,
            "fresh_tokens": _summary(fresh_tokens, n),
            "context_bloat_proxy_input_tokens_per_turn": _summary(context_bloat, n),
            "cache_read_share_of_input": _summary(cache_share, n),
        },
        "efficiency": {
            "success_per_fresh_token": (
                successes / total_fresh if total_fresh not in (None, 0) else None
            ),
            "success_per_turn": (
                successes / total_turns if total_turns not in (None, 0) else None
            ),
            "success_per_action": (
                successes / total_actions if total_actions not in (None, 0) else None
            ),
        },
        "trial_failure_counts": dict(
            sorted(Counter(str(row["failure_class"]) for row in rows).items())
        ),
        "retry": {
            "total": sum(
                int(row["candidate_provenance"]["retry_count"]) for row in rows
            ),
            **_summary(
                [row["candidate_provenance"]["retry_count"] for row in rows], n
            ),
        },
        "focus_event_count": {
            "total": sum(
                int(row["candidate_provenance"]["focus_event_count"]) for row in rows
            ),
            **_summary(
                [row["candidate_provenance"]["focus_event_count"] for row in rows], n
            ),
        },
        "mcp": mcp,
        "model": model,
        "attribution": {
            "available_n": sum(value is not None for value in trial_attribution),
            "missing_n": sum(value is None for value in trial_attribution),
            "trial_totals": [
                value for value in trial_attribution if value is not None
            ],
            "metrics": {
                field: _summary(
                    [value[field] for value in flat_attribution], n
                )
                for field in _ATTRIBUTION_FIELDS
            },
            "perception_phase_time_ms": {
                name: _summary(
                    [
                        (
                            value["mcp_detail_ms"]["perception_phase_time_ms"].get(name)
                            if value is not None
                            else None
                        )
                        for value in trial_attribution
                    ],
                    n,
                )
                for name in phase_names
            },
        },
        "unavailable_metrics": [*missing, *model_missing],
    }


def _matched_deltas(
    reference: Sequence[_Observation],
    candidate: Sequence[_Observation],
) -> dict[str, Any]:
    reference_observations = {int(item.row["trial"]): item for item in reference}
    candidate_observations = {int(item.row["trial"]): item for item in candidate}
    reference_by_trial = {trial: item.row for trial, item in reference_observations.items()}
    candidate_by_trial = {trial: item.row for trial, item in candidate_observations.items()}
    trials = sorted(set(reference_by_trial) & set(candidate_by_trial))
    fields = {"success": "success", **_METRIC_FIELDS, "fresh_tokens": "tokens"}
    metrics: dict[str, Any] = {}
    for public_name, row_field in fields.items():
        deltas: list[float] = []
        missing = 0
        for trial in trials:
            left = reference_by_trial[trial].get(row_field)
            right = candidate_by_trial[trial].get(row_field)
            if public_name == "success":
                left = float(bool(left))
                right = float(bool(right))
            else:
                left = _number(left)
                right = _number(right)
            if left is None or right is None:
                missing += 1
            else:
                deltas.append(float(right) - float(left))
        metrics[public_name] = {
            "n": len(deltas),
            "missing_n": missing,
            "mean": sum(deltas) / len(deltas) if deltas else None,
            "median": median(deltas) if deltas else None,
            "p95": _percentile(deltas, 0.95),
        }
    for field in _ATTRIBUTION_FIELDS:
        deltas = []
        missing = 0
        for trial in trials:
            left = _flat_attribution(
                _trial_attribution(reference_observations[trial])
            )[field]
            right = _flat_attribution(
                _trial_attribution(candidate_observations[trial])
            )[field]
            if left is None or right is None:
                missing += 1
            else:
                deltas.append(float(right) - float(left))
        metrics[field] = {
            "n": len(deltas),
            "missing_n": missing,
            "mean": sum(deltas) / len(deltas) if deltas else None,
            "median": median(deltas) if deltas else None,
            "p95": _percentile(deltas, 0.95),
        }
    phase_names = sorted({
        name
        for trial in trials
        for observation in (
            reference_observations[trial],
            candidate_observations[trial],
        )
        for attribution in (_trial_attribution(observation),)
        if attribution is not None
        for name in attribution["mcp_detail_ms"]["perception_phase_time_ms"]
    })
    for name in phase_names:
        deltas = []
        missing = 0
        for trial in trials:
            left_attribution = _trial_attribution(reference_observations[trial])
            right_attribution = _trial_attribution(candidate_observations[trial])
            left = (
                left_attribution["mcp_detail_ms"]["perception_phase_time_ms"].get(name)
                if left_attribution is not None
                else None
            )
            right = (
                right_attribution["mcp_detail_ms"]["perception_phase_time_ms"].get(name)
                if right_attribution is not None
                else None
            )
            if left is None or right is None:
                missing += 1
            else:
                deltas.append(float(right) - float(left))
        metrics[f"perception_phase_time_ms.{name}"] = {
            "n": len(deltas),
            "missing_n": missing,
            "mean": sum(deltas) / len(deltas) if deltas else None,
            "median": median(deltas) if deltas else None,
            "p95": _percentile(deltas, 0.95),
        }
    success_deltas = [
        int(candidate_by_trial[trial]["success"])
        - int(reference_by_trial[trial]["success"])
        for trial in trials
    ]
    return {
        "matched_n": len(trials),
        "candidate_minus_reference": metrics,
        "success_pairs": {
            "wins": sum(delta > 0 for delta in success_deltas),
            "ties": sum(delta == 0 for delta in success_deltas),
            "losses": sum(delta < 0 for delta in success_deltas),
        },
    }


def build_native_report(
    plan: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any] | str | Path],
) -> dict[str, Any]:
    """Build a public report from strict native rows and/or validated bundles."""
    validated_plan = validate_native_matrix(plan)
    if not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)):
        raise NativeReportError("inputs must be a sequence of rows or bundle paths")
    observations_by_cell: dict[str, _Observation] = {}
    arm_by_cell: dict[str, str] = {}
    cell_by_id = {cell["cell_id"]: cell for cell in validated_plan["schedule"]}
    for raw in inputs:
        observation = _load_observation(raw)
        arm, cell = _match_cell(validated_plan, observation)
        previous = observations_by_cell.get(cell["cell_id"])
        if previous is not None:
            observations_by_cell[cell["cell_id"]] = _merge_observation(
                previous,
                observation,
                cell_id=cell["cell_id"],
            )
            continue
        observations_by_cell[cell["cell_id"]] = observation
        arm_by_cell[cell["cell_id"]] = arm["id"]

    arm_ids = [arm["id"] for arm in validated_plan["arms"]]
    complete_blocks: list[int] = []
    incomplete_blocks: list[dict[str, Any]] = []
    for block in range(1, validated_plan["repetitions"] + 1):
        missing = [
            f"block{block}:{arm_id}"
            for arm_id in arm_ids
            if f"block{block}:{arm_id}" not in observations_by_cell
        ]
        noncompleted = [
            f"block{block}:{arm_id}"
            for arm_id in arm_ids
            if f"block{block}:{arm_id}" in observations_by_cell
            and observations_by_cell[f"block{block}:{arm_id}"].row["completed"] is not True
        ]
        if missing or noncompleted:
            incomplete = {"block": block, "missing_cell_ids": missing}
            if noncompleted:
                incomplete["noncompleted_cell_ids"] = noncompleted
            incomplete_blocks.append(incomplete)
        else:
            complete_blocks.append(block)

    matched: dict[str, list[_Observation]] = {arm_id: [] for arm_id in arm_ids}
    for cell_id, observation in observations_by_cell.items():
        if cell_by_id[cell_id]["block"] in complete_blocks:
            matched[arm_by_cell[cell_id]].append(observation)
    for arm_id in arm_ids:
        matched[arm_id].sort(key=lambda item: int(item.row["trial"]))

    evidence = [
        {
            "cell_id": cell_id,
            "cell_sha256": cell_by_id[cell_id]["cell_sha256"],
            "trial_id": observation.row["candidate_provenance"]["trial_id"],
            "config_sha256": cell_by_id[cell_id]["config_sha256"],
            "bundle_sha256": observation.bundle_sha256,
            "result_sha256": observation.result_sha256,
            "normalized_row_sha256": observation.row_sha256,
            "bundle_validated": observation.mcp_calls is not None,
        }
        for cell_id, observation in sorted(
            observations_by_cell.items(),
            key=lambda item: cell_by_id[item[0]]["sequence"],
        )
    ]
    reference_id = arm_ids[0]
    row_only_cells = sum(
        observation.mcp_calls is None for observation in observations_by_cell.values()
    )
    arm_aggregates = {
        arm_id: _aggregate_observations(matched[arm_id]) for arm_id in arm_ids
    }
    for aggregate in arm_aggregates.values():
        aggregate["attribution"].pop("trial_totals", None)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "comparison_id": validated_plan["comparison_id"],
        "publication_status": (
            "incomplete_noncomparable_cells_excluded"
            if incomplete_blocks
            else "complete_bundle_validated"
            if row_only_cells == 0
            else "complete_row_bound_bundle_not_revalidated"
        ),
        "methodology": {
            **validated_plan["methodology"],
            "continuous_percentile": "nearest_rank",
            "matched_delta_direction": "candidate_minus_reference",
            "raw_evidence_included": False,
            "operator_evidence_attested": False,
            "row_evidence_binding": "canonical_normalized_row_sha256",
        },
        "identities": {
            "plan_sha256": validated_plan["plan_sha256"],
            "fixed_identity_sha256": validated_plan["fixed_identity_sha256"],
            "native_bundle_schema": BUNDLE_SCHEMA_VERSION,
            "arm_config_sha256": {
                arm["id"]: arm["config_sha256"] for arm in validated_plan["arms"]
            },
        },
        "coverage": {
            "planned_blocks": validated_plan["repetitions"],
            "complete_matched_blocks": len(complete_blocks),
            "complete_block_ids": complete_blocks,
            "incomplete_blocks": incomplete_blocks,
            "observed_cells": len(observations_by_cell),
            "planned_cells": len(validated_plan["schedule"]),
            "bundle_validated_cells": len(observations_by_cell) - row_only_cells,
            "row_only_cells": row_only_cells,
            "publish_repetition_recommendation_met": validated_plan[
                "publish_repetition_recommendation_met"
            ],
            "publish_recommended_repetitions": validated_plan[
                "publish_recommended_repetitions"
            ],
        },
        "arms": arm_aggregates,
        "matched_deltas": {
            arm_id: _matched_deltas(matched[reference_id], matched[arm_id])
            for arm_id in arm_ids[1:]
        },
        "evidence_digests": evidence,
    }
    report["report_sha256"] = canonical_sha256(report)
    assert_public_native_report(report)
    return report


__all__ = [
    "NativeReportError",
    "REPORT_SCHEMA_VERSION",
    "assert_public_native_report",
    "build_native_report",
]
