"""Identity binding and durable JSONL storage for Gateway Probe results."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import gateway_spec
from . import gateway_probe_spec as probe_spec
from .gateway_probe_models import GatewayProbeRunError, ProbeBlock


BENCHMARK = "gateway_probe"
RESULT_SCHEMA_VERSION = 2


def block_id(
    experiment_digest: str,
    block: ProbeBlock,
    attempt: int,
) -> str:
    block_material = {
        "experiment_digest": experiment_digest,
        "case_id": block.case_id,
        "condition": block.condition,
        "repetition": block.repetition,
        "attempt": attempt,
    }
    return "gateway-probe-block-" + gateway_spec.canonical_digest(block_material)


def make_identity(
    experiment: probe_spec.GatewayProbeExperiment,
    arm: gateway_spec.Arm,
    block: ProbeBlock,
    attempt: int,
    schedule_digest: str,
    price_digest: str,
) -> dict[str, Any]:
    return {
        "benchmark": {"name": BENCHMARK, "track": probe_spec.TRACK},
        "experiment": {"id": experiment.experiment_id, "digest": experiment.digest},
        "arm": {"id": arm.arm_id, "digest": arm.digest},
        "case": {"id": block.case_id, "prompt_digest": block.prompt_digest},
        "comparison": {
            "schedule_digest": schedule_digest,
            "price_digest": price_digest,
        },
        "schedule": {
            "condition": block.condition,
            "repetition": block.repetition,
            "block_id": block_id(experiment.digest, block, attempt),
            "block_attempt": attempt,
        },
    }


def cell_id(identity: Mapping[str, Any]) -> str:
    return "gateway-probe-cell-v2-" + gateway_spec.canonical_digest(identity)


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise GatewayProbeRunError("results JSONL has an unterminated final line")
    rows = []
    seen = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line:
            raise GatewayProbeRunError(
                f"results JSONL has a blank line at {line_number}"
            )
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GatewayProbeRunError(
                f"results JSONL line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(row, dict) or row.get("benchmark") != BENCHMARK:
            raise GatewayProbeRunError("results JSONL contains a non-probe row")
        row_cell_id = row.get("cell_id")
        if not isinstance(row_cell_id, str) or row_cell_id in seen:
            raise GatewayProbeRunError(
                "results JSONL contains an invalid or duplicate cell_id"
            )
        seen.add(row_cell_id)
        rows.append(row)
    return rows


def load_results(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    return read_rows(Path(path))


def _exact_mapping(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise GatewayProbeRunError(f"results row has malformed {label}")
    return value


def _validate_result_shape(row: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version", "benchmark", "cell_id", "identity",
        "expected_arm_ids", "scheduled_blocks_per_condition", "arm_role",
        "baseline", "model_match", "outcome", "route_integrity",
        "request_metrics", "reuse_evidence", "billing",
    }
    if set(row) != expected_fields:
        raise GatewayProbeRunError("results row does not match schema v2")
    outcome = _exact_mapping(
        row.get("outcome"),
        {
            "attempted", "success", "available", "http_status", "timed_out",
            "error_class", "error_detail", "budget_exhausted_reason",
        },
        "outcome",
    )
    if any(
        not isinstance(outcome.get(field), bool)
        for field in ("attempted", "success", "available", "timed_out")
    ):
        raise GatewayProbeRunError("results row outcome is malformed")
    if outcome.get("http_status") is not None and (
        not isinstance(outcome.get("http_status"), int)
        or isinstance(outcome.get("http_status"), bool)
    ):
        raise GatewayProbeRunError("results row outcome is malformed")
    error_classes = {None, "timeout", "primer", "transport", "http", "stream"}
    error_details = {
        None, "timeout", "primer_invalid", "bad_status_line", "http_protocol",
        "tls", "dns", "connection_refused", "connection_reset",
        "connection_closed", "probe_policy", "network_io", "internal",
        "http_status", "stream_terminal", "stream_incomplete",
    }
    if (
        outcome.get("error_class") not in error_classes
        or outcome.get("error_detail") not in error_details
        or (
            outcome.get("budget_exhausted_reason") is not None
            and not isinstance(outcome.get("budget_exhausted_reason"), str)
        )
    ):
        raise GatewayProbeRunError("results row outcome taxonomy is malformed")
    route = _exact_mapping(
        row.get("route_integrity"),
        {"status", "pass", "reasons"},
        "route integrity",
    )
    if (
        route.get("status") not in {"verified", "unverifiable", "failed"}
        or route.get("pass") is not (route.get("status") == "verified")
        or not isinstance(route.get("reasons"), list)
        or any(not isinstance(reason, str) for reason in route["reasons"])
    ):
        raise GatewayProbeRunError("results row route integrity is malformed")
    _exact_mapping(
        row.get("request_metrics"),
        {
            "connection", "timing", "usage", "generation", "cache",
            "route", "costs", "stream", "coverage",
        },
        "request metrics",
    )
    _exact_mapping(
        row.get("reuse_evidence"),
        {
            "required", "completed", "http_status", "socket_reused",
            "primer_nonce_sha256", "measured_nonce_sha256", "connection",
            "route_integrity", "usage", "cache", "costs",
        },
        "reuse evidence",
    )
    billing = _exact_mapping(
        row.get("billing"),
        {
            "primer_cost_usd", "measured_cost_usd",
            "charged_cost_usd", "stop_required",
        },
        "billing evidence",
    )
    if not isinstance(billing.get("stop_required"), bool):
        raise GatewayProbeRunError("results row billing evidence is malformed")


def validate_resume_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    experiment: probe_spec.GatewayProbeExperiment,
    schedule: Sequence[ProbeBlock],
    schedule_digest: str,
    price_digest: str,
) -> None:
    expected_arm_ids = sorted(arm.arm_id for arm in experiment.arms)
    arms_by_id = {arm.arm_id: arm for arm in experiment.arms}
    cases_by_id = {case.case_id: case for case in experiment.cases}
    blocks_by_coordinate = {block.coordinate: block for block in schedule}
    scheduled_per_condition = len(experiment.cases) * experiment.repetitions
    logical_rows = set()
    for row in rows:
        if row.get("schema_version") != RESULT_SCHEMA_VERSION:
            raise GatewayProbeRunError("results row has unsupported schema_version")
        _validate_result_shape(row)
        identity = _exact_mapping(
            row.get("identity"),
            {"benchmark", "experiment", "arm", "case", "comparison", "schedule"},
            "identity",
        )
        try:
            expected_cell_id = cell_id(identity)
        except (TypeError, ValueError, gateway_spec.GatewaySpecError) as exc:
            raise GatewayProbeRunError(
                "results row identity is not canonical JSON"
            ) from exc
        if row.get("cell_id") != expected_cell_id:
            raise GatewayProbeRunError("results row cell_id does not match identity")
        benchmark = _exact_mapping(
            identity.get("benchmark"), {"name", "track"}, "benchmark identity"
        )
        if benchmark != {"name": BENCHMARK, "track": probe_spec.TRACK}:
            raise GatewayProbeRunError(
                "results row benchmark identity does not match"
            )
        experiment_identity = _exact_mapping(
            identity.get("experiment"), {"id", "digest"}, "experiment identity"
        )
        if experiment_identity != {
            "id": experiment.experiment_id,
            "digest": experiment.digest,
        }:
            raise GatewayProbeRunError(
                "results row experiment identity does not match"
            )
        comparison = _exact_mapping(
            identity.get("comparison"),
            {"schedule_digest", "price_digest"},
            "comparison identity",
        )
        if comparison != {
            "schedule_digest": schedule_digest,
            "price_digest": price_digest,
        }:
            raise GatewayProbeRunError(
                "results row comparison digests do not match"
            )
        arm_identity = _exact_mapping(
            identity.get("arm"), {"id", "digest"}, "arm identity"
        )
        arm_id = arm_identity.get("id")
        if not isinstance(arm_id, str):
            raise GatewayProbeRunError("results row arm identity is invalid")
        arm = arms_by_id.get(arm_id)
        if arm is None or arm_identity.get("digest") != arm.digest:
            raise GatewayProbeRunError("results row arm identity does not match")
        case_identity = _exact_mapping(
            identity.get("case"), {"id", "prompt_digest"}, "case identity"
        )
        case_id = case_identity.get("id")
        if not isinstance(case_id, str):
            raise GatewayProbeRunError("results row case identity is invalid")
        case = cases_by_id.get(case_id)
        if case is None or case_identity.get("prompt_digest") != case.prompt_digest:
            raise GatewayProbeRunError("results row case identity does not match")
        schedule_identity = _exact_mapping(
            identity.get("schedule"),
            {"condition", "repetition", "block_id", "block_attempt"},
            "schedule identity",
        )
        condition = schedule_identity.get("condition")
        repetition = schedule_identity.get("repetition")
        attempt = schedule_identity.get("block_attempt")
        if (
            not isinstance(condition, str)
            or not isinstance(repetition, int)
            or isinstance(repetition, bool)
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 0
        ):
            raise GatewayProbeRunError(
                "results row schedule identity is invalid"
            )
        coordinate = (case_id, condition, repetition)
        block = blocks_by_coordinate.get(coordinate)
        if block is None or arm_id not in block.arm_ids:
            raise GatewayProbeRunError(
                "results row is not a scheduled arm membership"
            )
        if schedule_identity.get("block_id") != block_id(
            experiment.digest, block, attempt
        ):
            raise GatewayProbeRunError(
                "results row block_id does not match schedule"
            )
        if row.get("expected_arm_ids") != expected_arm_ids:
            raise GatewayProbeRunError(
                "results row expected_arm_ids do not match"
            )
        if row.get("scheduled_blocks_per_condition") != scheduled_per_condition:
            raise GatewayProbeRunError(
                "results row scheduled block count does not match"
            )
        if (
            row.get("arm_role") != arm.route_kind
            or row.get("baseline") is not arm.baseline
            or row.get("model_match") != experiment.model_match
        ):
            raise GatewayProbeRunError(
                "results row arm provenance does not match"
            )
        logical_key = (case_id, condition, repetition, attempt, arm_id)
        if logical_key in logical_rows:
            raise GatewayProbeRunError(
                "results contain a duplicate logical arm row"
            )
        logical_rows.add(logical_key)


def append_row(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        row,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def charged_cost(row: Mapping[str, Any]) -> Decimal:
    billing = row.get("billing")
    if billing is None:
        return Decimal(0)
    if not isinstance(billing, Mapping):
        raise GatewayProbeRunError(
            "results row has malformed billing evidence"
        )
    raw = billing.get("charged_cost_usd")
    try:
        amount = Decimal(str(raw))
    except (ArithmeticError, ValueError) as exc:
        raise GatewayProbeRunError(
            "results row has malformed charged cost"
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise GatewayProbeRunError("results row has malformed charged cost")
    return amount
