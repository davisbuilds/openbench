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

from .mcp_stdio_collector import verify_ledger
from .native_matrix import canonical_sha256, validate_native_matrix
from .native_trial import BUNDLE_SCHEMA_VERSION, load_native_trial
from .run import ROW_FIELDS
from .stats import wilson_ci


REPORT_SCHEMA_VERSION = "openbench.native-report.v1"
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


class NativeReportError(ValueError):
    """Raised when native comparison evidence is unsafe or non-comparable."""


@dataclass(frozen=True)
class _Observation:
    row: Mapping[str, Any]
    mcp_calls: tuple[Mapping[str, Any], ...] | None
    bundle_sha256: str
    result_sha256: str


def _number(value: Any) -> float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    ):
        return float(value)
    return None


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
    return _Observation(
        row=row,
        mcp_calls=calls,
        bundle_sha256=provenance["manifest_sha256"],
        result_sha256=provenance["result_sha256"],
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
    )


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
    all_latencies = [value for values in latencies.values() for value in values]
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
            "error_counts": dict(sorted(errors.items())),
            "outcome_counts": dict(sorted(outcomes.items())),
            "delivery_counts": dict(sorted(deliveries.items())),
            "focus_counts": dict(sorted(focus.items())),
        },
        [],
    )


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
        "unavailable_metrics": missing,
    }


def _matched_deltas(
    reference: Sequence[_Observation],
    candidate: Sequence[_Observation],
) -> dict[str, Any]:
    reference_by_trial = {
        int(item.row["trial"]): item.row for item in reference
    }
    candidate_by_trial = {
        int(item.row["trial"]): item.row for item in candidate
    }
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
            if (
                previous.result_sha256 != observation.result_sha256
                or previous.bundle_sha256 != observation.bundle_sha256
            ):
                raise NativeReportError(
                    f"planned cell {cell['cell_id']!r} has conflicting results"
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
        if missing:
            incomplete_blocks.append({"block": block, "missing_cell_ids": missing})
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
        }
        for cell_id, observation in sorted(
            observations_by_cell.items(),
            key=lambda item: cell_by_id[item[0]]["sequence"],
        )
    ]
    reference_id = arm_ids[0]
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "comparison_id": validated_plan["comparison_id"],
        "publication_status": (
            "complete"
            if not incomplete_blocks
            else "incomplete_noncomparable_cells_excluded"
        ),
        "methodology": {
            **validated_plan["methodology"],
            "continuous_percentile": "nearest_rank",
            "matched_delta_direction": "candidate_minus_reference",
            "raw_evidence_included": False,
            "operator_evidence_attested": False,
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
            "publish_repetition_recommendation_met": validated_plan[
                "publish_repetition_recommendation_met"
            ],
            "publish_recommended_repetitions": validated_plan[
                "publish_recommended_repetitions"
            ],
        },
        "arms": {
            arm_id: _aggregate_observations(matched[arm_id]) for arm_id in arm_ids
        },
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
