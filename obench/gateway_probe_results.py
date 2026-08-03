"""Identity binding and durable JSONL storage for Gateway Probe results."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import gateway_spec
from . import gateway_probe_spec as probe_spec
from .gateway_probe_models import GatewayProbeRunError, ProbeBlock


BENCHMARK = "gateway_probe"
# V4 admits additive primer route and retry receipt evidence so checked-in
# published bundles remain independently verifiable.
RESULT_SCHEMA_VERSION = 4
RECEIPT_HEADER_ALLOWLIST = frozenset({
    "x-request-id",
    "request-id",
    "openai-request-id",
    "anthropic-request-id",
    "x-vercel-id",
    "cf-ray",
})
RECEIPT_VALUE_MAX_LENGTH = 256
_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._:/@+\-]{1,256}")
RECEIPT_VALUE_RE = re.compile(r"[A-Za-z0-9._:/@+=\-]{1,256}")
_NORMALIZED_REASON_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
_OUTPUT_TOKEN_DETAIL_KEYS = frozenset({
    "accepted_prediction_tokens",
    "audio_tokens",
    "image_tokens",
    "reasoning_tokens",
    "rejected_prediction_tokens",
    "text_tokens",
    "video_tokens",
})
_PRIMER_STREAM_FIELDS = frozenset({
    "done",
    "terminal_status",
    "finish_reason",
    "finalized",
})


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
    return "gateway-probe-cell-v4-" + gateway_spec.canonical_digest(identity)


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
        validate_row_shape(row)
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


def _duration(value: Any) -> bool:
    return (
        value is None
        or (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )
    )


def _validate_timing_order(
    timing: Mapping[str, Any],
    *,
    condition: str,
) -> None:
    request_names = (
        "request_to_response_headers_s",
        "request_to_first_body_byte_s",
        "request_to_semantic_ttft_s",
        "request_stream_total_s",
    )
    cold_names = (
        "cold_end_to_end_response_headers_s",
        "cold_end_to_end_first_body_byte_s",
        "cold_end_to_end_semantic_ttft_s",
        "cold_end_to_end_stream_total_s",
    )

    def ordered(names: Sequence[str]) -> bool:
        observed = [
            timing.get(name)
            for name in names
            if timing.get(name) is not None
        ]
        return observed == sorted(observed)

    if not ordered(request_names):
        raise GatewayProbeRunError("results row request timing is out of order")
    if condition == "warm":
        if any(timing.get(name) is not None for name in cold_names):
            raise GatewayProbeRunError(
                "warm request contains cold timing evidence"
            )
        return
    if not ordered(cold_names):
        raise GatewayProbeRunError(
            "results row cold end-to-end timing is out of order"
        )
    offsets = []
    for request_name, cold_name in zip(request_names, cold_names):
        request_value = timing.get(request_name)
        cold_value = timing.get(cold_name)
        if (request_value is None) is not (cold_value is None):
            raise GatewayProbeRunError(
                "cold request timing coverage is inconsistent"
            )
        if request_value is not None:
            if cold_value < request_value:
                raise GatewayProbeRunError(
                    "cold end-to-end timing is shorter than request timing"
                )
            offsets.append(cold_value - request_value)
    if offsets and max(offsets) - min(offsets) > 1e-6:
        raise GatewayProbeRunError(
            "cold end-to-end timing has inconsistent setup offsets"
        )


def _validate_setup(value: Any, label: str, *, nullable: bool) -> None:
    if value is None and nullable:
        return
    setup = _exact_mapping(value, {"dns_s", "tcp_s", "tls_s"}, label)
    if any(not _duration(setup.get(name)) for name in setup):
        raise GatewayProbeRunError(f"results row has malformed {label}")


def _validate_receipts(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise GatewayProbeRunError(f"results row has malformed {label}")
    if any(name not in RECEIPT_HEADER_ALLOWLIST for name in value):
        raise GatewayProbeRunError(f"results row has unsafe {label}")
    for receipt in value.values():
        if (
            not isinstance(receipt, str)
            or not receipt
            or len(receipt) > RECEIPT_VALUE_MAX_LENGTH
            or receipt != receipt.strip()
            or RECEIPT_VALUE_RE.fullmatch(receipt) is None
        ):
            raise GatewayProbeRunError(f"results row has unsafe {label}")


def _count(value: Any, *, nullable: bool = True) -> bool:
    return (
        value is None and nullable
    ) or (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _safe_identifier(value: Any, *, nullable: bool = True) -> bool:
    return (
        value is None and nullable
    ) or (
        isinstance(value, str)
        and _SAFE_IDENTIFIER_RE.fullmatch(value) is not None
    )


def _cost_scalar(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return math.isfinite(value) and value >= 0
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        amount = Decimal(value)
    except (ArithmeticError, ValueError):
        return False
    return amount.is_finite() and amount >= 0


def _validate_usage(value: Any, label: str) -> None:
    if value is None:
        return
    usage = _exact_mapping(
        value,
        set(value) if isinstance(value, Mapping) else set(),
        label,
    )
    allowed = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "input_tokens_details",
        "output_tokens_details",
    }
    if not set(usage) <= allowed or any(
        not _count(usage.get(name))
        for name in ("input_tokens", "output_tokens", "total_tokens")
        if name in usage
    ):
        raise GatewayProbeRunError(f"results row has malformed {label}")
    details = usage.get("input_tokens_details")
    if details is not None:
        details = _exact_mapping(
            details,
            set(details) if isinstance(details, Mapping) else set(),
            f"{label} details",
        )
        if (
            not set(details) <= {
                "cached_tokens",
                "cache_write_tokens",
                "cached_tokens_created",
            }
            or any(not _count(item, nullable=False) for item in details.values())
        ):
            raise GatewayProbeRunError(
                f"results row has malformed {label} details"
            )
    output_details = usage.get("output_tokens_details")
    if output_details is not None:
        output_details = _exact_mapping(
            output_details,
            (
                set(output_details)
                if isinstance(output_details, Mapping)
                else set()
            ),
            f"{label} output details",
        )
        if (
            not set(output_details) <= _OUTPUT_TOKEN_DETAIL_KEYS
            or any(
                not _count(item, nullable=False)
                for item in output_details.values()
            )
        ):
            raise GatewayProbeRunError(
                f"results row has malformed {label} output details"
            )


def _validate_generation(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or not set(value) <= {
        "output_tokens", "duration_s", "tokens_per_second",
    }:
        raise GatewayProbeRunError(f"results row has malformed {label}")
    if "output_tokens" in value and not _count(value["output_tokens"]):
        raise GatewayProbeRunError(f"results row has malformed {label}")
    if any(
        not _duration(value[name])
        for name in ("duration_s", "tokens_per_second")
        if name in value
    ):
        raise GatewayProbeRunError(f"results row has malformed {label}")


def _validate_cache(value: Any, label: str, *, nullable: bool) -> None:
    if value is None and nullable:
        return
    cache = _exact_mapping(
        value,
        {"cached_input_tokens", "cache_write_input_tokens"},
        label,
    )
    if any(not _count(item) for item in cache.values()):
        raise GatewayProbeRunError(f"results row has malformed {label}")


def _validate_route(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or not set(value) <= {
        "requested_model",
        "metadata_requested_model",
        "served_model",
        "provider",
        "attempts",
        "gateway_metadata",
    }:
        raise GatewayProbeRunError(f"results row has malformed {label}")
    for name in (
        "requested_model",
        "metadata_requested_model",
        "served_model",
        "provider",
    ):
        if name in value and not _safe_identifier(value[name]):
            raise GatewayProbeRunError(f"results row has unsafe {label}")
    attempts = value.get("attempts")
    if attempts is not None:
        if not isinstance(attempts, list) or len(attempts) > 64:
            raise GatewayProbeRunError(f"results row has malformed {label}")
        for attempt in attempts:
            if not isinstance(attempt, Mapping) or not set(attempt) <= {
                "provider", "model", "status",
            }:
                raise GatewayProbeRunError(f"results row has malformed {label}")
            if (
                "provider" in attempt
                and not _safe_identifier(attempt["provider"], nullable=False)
            ) or (
                "model" in attempt
                and not _safe_identifier(attempt["model"], nullable=False)
            ) or (
                "status" in attempt
                and not _count(attempt["status"], nullable=False)
            ):
                raise GatewayProbeRunError(f"results row has unsafe {label}")
    metadata = value.get("gateway_metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping) or not set(metadata) <= {
            "cost", "marketCost", "generationId",
        }:
            raise GatewayProbeRunError(f"results row has malformed {label}")
        for name, item in metadata.items():
            if name == "generationId":
                valid = _safe_identifier(item, nullable=False)
            else:
                valid = _cost_scalar(item)
            if not valid:
                raise GatewayProbeRunError(f"results row has unsafe {label}")


def _validate_costs(value: Any, label: str) -> None:
    if not isinstance(value, Mapping) or not set(value) <= {
        "gateway_reported", "frozen_list_estimate",
    }:
        raise GatewayProbeRunError(f"results row has malformed {label}")
    for item in value.values():
        evidence = _exact_mapping(
            item,
            {"amount_usd", "currency", "effective_at"},
            label,
        )
        if (
            not _duration(evidence.get("amount_usd"))
            or evidence.get("amount_usd") is None
            or evidence.get("currency") != "USD"
            or not _safe_identifier(
                evidence.get("effective_at"), nullable=False
            )
        ):
            raise GatewayProbeRunError(f"results row has malformed {label}")


def _validate_stream(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or not set(value) <= {
        "events",
        "ignored_events",
        "malformed_events",
        "done",
        "terminal_status",
        "finish_reason",
        "finalized",
    }:
        raise GatewayProbeRunError(f"results row has malformed {label}")
    for name in ("events", "ignored_events", "malformed_events"):
        if name in value and not _count(value[name], nullable=False):
            raise GatewayProbeRunError(f"results row has malformed {label}")
    for name in ("done", "finalized"):
        if name in value and not isinstance(value[name], bool):
            raise GatewayProbeRunError(f"results row has malformed {label}")
    if "terminal_status" in value and not _safe_identifier(value["terminal_status"]):
        raise GatewayProbeRunError(f"results row has unsafe {label}")
    finish_reason = value.get("finish_reason")
    if finish_reason is not None and (
        not isinstance(finish_reason, str)
        or len(finish_reason) > 64
        or _NORMALIZED_REASON_RE.fullmatch(finish_reason) is None
    ):
        raise GatewayProbeRunError(f"results row has unsafe {label}")


def _validate_primer_stream(value: Any, label: str) -> None:
    if value is None:
        return
    stream = _exact_mapping(value, set(_PRIMER_STREAM_FIELDS), label)
    _validate_stream(stream, label)


def _validate_outcome_evidence(
    outcome: Mapping[str, Any],
    timing: Mapping[str, Any],
    stream: Any,
) -> None:
    timed_out = outcome.get("timed_out")
    error_class = outcome.get("error_class")
    error_detail = outcome.get("error_detail")
    if timed_out is not (error_class == "timeout"):
        raise GatewayProbeRunError("results row timeout evidence is inconsistent")
    if (error_class is None) is not (error_detail is None):
        raise GatewayProbeRunError("results row error evidence is inconsistent")

    attempted = outcome.get("attempted")
    success = outcome.get("success")
    status = outcome.get("http_status")
    request_names = (
        "request_to_response_headers_s",
        "request_to_first_body_byte_s",
        "request_to_semantic_ttft_s",
        "request_stream_total_s",
    )
    if not attempted:
        if (
            success
            or status is not None
            or stream is not None
            or any(timing.get(name) is not None for name in request_names)
        ):
            raise GatewayProbeRunError(
                "unattempted request contains response evidence"
            )
        return
    if success:
        if (
            not isinstance(status, int)
            or not 200 <= status < 300
            or timed_out
            or error_class is not None
            or any(timing.get(name) is None for name in request_names)
            or not isinstance(stream, Mapping)
            or stream.get("done") is not True
            or stream.get("finalized") is not True
            or stream.get("terminal_status") not in {None, "completed"}
        ):
            raise GatewayProbeRunError(
                "successful request evidence is inconsistent"
            )
    elif error_class is None:
        raise GatewayProbeRunError(
            "unsuccessful attempted request has no error evidence"
        )


def _validate_coverage(value: Any, label: str) -> None:
    if value is None:
        return
    allowed_flags = {
        "ttfb",
        "semantic_ttft",
        "usage",
        "generation",
        "requested_model",
        "served_model",
        "openrouter_metadata",
        "provider",
        "attempts",
        "attempt_evidence",
        "stream_done",
    }
    if not isinstance(value, Mapping) or not set(value) <= (
        allowed_flags | {"covered", "total", "ratio"}
    ):
        raise GatewayProbeRunError(f"results row has malformed {label}")
    if any(
        not isinstance(value[name], bool)
        for name in allowed_flags
        if name in value
    ):
        raise GatewayProbeRunError(f"results row has malformed {label}")
    if any(
        not _count(value[name], nullable=False)
        for name in ("covered", "total")
        if name in value
    ):
        raise GatewayProbeRunError(f"results row has malformed {label}")
    ratio = value.get("ratio")
    if ratio is not None and (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not math.isfinite(ratio)
        or not 0 <= ratio <= 1
    ):
        raise GatewayProbeRunError(f"results row has malformed {label}")


def _validate_route_integrity(value: Any, label: str) -> None:
    if value is None:
        return
    route = _exact_mapping(value, {"status", "pass", "reasons"}, label)
    if (
        route.get("status") not in {"verified", "unverifiable", "failed"}
        or route.get("pass") is not (route.get("status") == "verified")
        or not isinstance(route.get("reasons"), list)
        or any(not _safe_identifier(reason, nullable=False) for reason in route["reasons"])
    ):
        raise GatewayProbeRunError(f"results row {label} is malformed")


def _recovered_primer_attempts_valid(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    attempts = value.get("attempts")
    if not isinstance(attempts, list) or len(attempts) < 2:
        return False
    normalized = []
    for attempt in attempts:
        if (
            not isinstance(attempt, Mapping)
            or set(attempt) != {"provider", "model", "status"}
            or not isinstance(attempt.get("provider"), str)
            or not attempt["provider"]
            or not isinstance(attempt.get("model"), str)
            or not attempt["model"]
            or not isinstance(attempt.get("status"), int)
            or isinstance(attempt.get("status"), bool)
        ):
            return False
        normalized.append((
            attempt["provider"],
            attempt["model"],
            attempt["status"],
        ))
    routes = {(provider, model) for provider, model, _status in normalized}
    successes = sum(200 <= status < 300 for _, _, status in normalized)
    return len(routes) == 1 and successes == 1


def _validate_money(value: Any, *, nullable: bool) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        amount = Decimal(value)
    except (ArithmeticError, ValueError):
        return False
    return amount.is_finite() and amount >= 0


def _validate_attempt_outcome(value: Any, label: str) -> Mapping[str, Any]:
    outcome = _exact_mapping(
        value,
        {
            "success",
            "http_status",
            "timed_out",
            "error_class",
            "error_detail",
            "semantic_output_started",
        },
        label,
    )
    if any(
        not isinstance(outcome.get(name), bool)
        for name in (
            "success",
            "timed_out",
            "semantic_output_started",
        )
    ):
        raise GatewayProbeRunError(f"results row has malformed {label}")
    status = outcome.get("http_status")
    if status is not None and (
        not isinstance(status, int) or isinstance(status, bool)
    ):
        raise GatewayProbeRunError(f"results row has malformed {label}")
    if outcome.get("error_class") not in {
        None,
        "timeout",
        "primer",
        "transport",
        "http",
        "stream",
    } or outcome.get("error_detail") not in {
        None,
        "timeout",
        "primer_invalid",
        "bad_status_line",
        "http_protocol",
        "tls",
        "dns",
        "connection_refused",
        "connection_reset",
        "connection_closed",
        "probe_policy",
        "network_io",
        "internal",
        "http_status",
        "stream_terminal",
        "stream_incomplete",
        "stream_no_semantic_output",
    }:
        raise GatewayProbeRunError(f"results row has malformed {label}")
    if (
        (outcome.get("error_class") is None)
        is not (outcome.get("error_detail") is None)
        or outcome.get("timed_out")
        is not (outcome.get("error_class") == "timeout")
        or (
            outcome.get("success")
            and (
                not isinstance(status, int)
                or not 200 <= status < 300
                or outcome.get("error_class") is not None
            )
        )
    ):
        raise GatewayProbeRunError(f"results row has inconsistent {label}")
    return outcome


def _validate_retry_evidence(value: Any, *, condition: str) -> None:
    retry = _exact_mapping(
        value,
        {
            "max_total_attempts",
            "max_input_tokens",
            "max_output_tokens",
            "retry_deadline_s",
            "reservation_input_per_million_usd",
            "reservation_output_per_million_usd",
            "attempt_count",
            "recovered",
            "first_attempt_outcome",
            "eventual_outcome",
            "recovery_timing",
            "attempts",
        },
        "retry evidence",
    )
    max_attempts = retry.get("max_total_attempts")
    attempt_count = retry.get("attempt_count")
    attempts = retry.get("attempts")
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
        or not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or not 1 <= attempt_count <= max_attempts
        or not isinstance(retry.get("recovered"), bool)
        or (
            retry.get("max_input_tokens") is not None
            and (
                not isinstance(retry.get("max_input_tokens"), int)
                or isinstance(retry.get("max_input_tokens"), bool)
                or retry.get("max_input_tokens") < 1
            )
        )
        or not _validate_money(
            retry.get("reservation_input_per_million_usd"),
            nullable=False,
        )
        or not _validate_money(
            retry.get("reservation_output_per_million_usd"),
            nullable=False,
        )
        or not isinstance(retry.get("max_output_tokens"), int)
        or isinstance(retry.get("max_output_tokens"), bool)
        or retry.get("max_output_tokens") < 1
        or (
            retry.get("retry_deadline_s") is not None
            and (
                not isinstance(retry.get("retry_deadline_s"), int)
                or isinstance(retry.get("retry_deadline_s"), bool)
                or retry.get("retry_deadline_s") < 1
            )
        )
        or not isinstance(attempts, list)
        or len(attempts) != attempt_count
    ):
        raise GatewayProbeRunError("results row has malformed retry evidence")
    normalized_attempts = []
    for index, raw in enumerate(attempts, 1):
        attempt_fields = {
            "attempt_number", "phase", "outcome", "timing", "retry", "cost",
        }
        if isinstance(raw, Mapping) and "receipt_headers" in raw:
            attempt_fields.add("receipt_headers")
        if isinstance(raw, Mapping) and "primer_evidence" in raw:
            attempt_fields.add("primer_evidence")
        attempt = _exact_mapping(raw, attempt_fields, "retry attempt")
        if attempt.get("attempt_number") != index or attempt.get("phase") not in {
            "primer",
            "measured",
        }:
            raise GatewayProbeRunError("results row has malformed retry attempt")
        attempt_outcome = _validate_attempt_outcome(
            attempt.get("outcome"),
            "retry attempt outcome",
        )
        timing = _exact_mapping(
            attempt.get("timing"),
            {
                "initial_request_start_offset_s",
                "request_to_response_headers_s",
                "request_to_semantic_output_s",
                "attempt_total_s",
            },
            "retry attempt timing",
        )
        if any(not _duration(item) for item in timing.values()):
            raise GatewayProbeRunError(
                "results row has malformed retry attempt timing"
            )
        if "receipt_headers" in attempt:
            _validate_receipts(
                attempt.get("receipt_headers"),
                "retry attempt receipts",
            )
        if "primer_evidence" in attempt:
            if attempt.get("phase") != "primer":
                raise GatewayProbeRunError(
                    "measured retry attempt contains primer evidence"
                )
            primer_evidence = _exact_mapping(
                attempt.get("primer_evidence"),
                {"route_integrity", "route", "stream"},
                "retry attempt primer evidence",
            )
            _validate_route_integrity(
                primer_evidence.get("route_integrity"),
                "retry attempt primer route integrity",
            )
            _validate_route(
                primer_evidence.get("route"),
                "retry attempt primer route",
            )
            _validate_primer_stream(
                primer_evidence.get("stream"),
                "retry attempt primer stream",
            )
        decision = _exact_mapping(
            attempt.get("retry"),
            {
                "eligible",
                "retry_after_status",
                "retry_after_s",
                "wait_requested_s",
                "wait_actual_s",
                "not_retried_reason",
            },
            "retry attempt decision",
        )
        if (
            not isinstance(decision.get("eligible"), bool)
            or decision.get("retry_after_status") not in {
                "absent",
                "normalized",
                "malformed",
                "over_deadline",
            }
            or decision.get("not_retried_reason") not in {
                None,
                "attempt_limit",
                "budget",
                "deadline",
                "malformed_retry_after",
                "not_retryable",
                "semantic_output_started",
            }
            or any(
                not _duration(decision.get(name))
                for name in (
                    "retry_after_s",
                    "wait_requested_s",
                    "wait_actual_s",
                )
            )
            or (
                (decision.get("wait_requested_s") is None)
                is not (decision.get("wait_actual_s") is None)
            )
            or (
                decision.get("wait_requested_s") is not None
                and (
                    not decision.get("eligible")
                    or index == attempt_count
                )
            )
        ):
            raise GatewayProbeRunError(
                "results row has malformed retry attempt decision"
            )
        primer_invalid = (
            attempt.get("phase") == "primer"
            and attempt_outcome["error_class"] == "primer"
            and attempt_outcome["error_detail"] == "primer_invalid"
        )
        expected_eligible = bool(
            primer_invalid
            or (
                not attempt_outcome["semantic_output_started"]
                and (
                    attempt_outcome["http_status"] in {429, 502, 503, 504}
                    or attempt_outcome["timed_out"]
                    or (
                        attempt_outcome["error_class"] == "transport"
                        and attempt_outcome["error_detail"] in {
                            "connection_reset",
                            "connection_closed",
                        }
                    )
                )
            )
        )
        if decision["eligible"] is not expected_eligible:
            raise GatewayProbeRunError(
                "results row retry eligibility is inconsistent"
            )
        if (
            index < attempt_count
            and (
                not decision["eligible"]
                or decision["wait_requested_s"] is None
                or decision["not_retried_reason"] is not None
                or decision["retry_after_status"] in {
                    "malformed",
                    "over_deadline",
                }
            )
        ) or (
            index == attempt_count
            and (
                decision["wait_requested_s"] is not None
                or decision["not_retried_reason"] is None
            )
        ):
            raise GatewayProbeRunError(
                "results row retry sequence is inconsistent"
            )
        cost = _exact_mapping(
            attempt.get("cost"),
            {
                "primer_cost_usd",
                "measured_cost_usd",
                "observed_cost_usd",
                "known_observed_cost_usd",
                "budget_debit_usd",
                "reservation_usd",
                "cost_status",
            },
            "retry attempt cost",
        )
        if (
            cost.get("cost_status") not in {"observed", "reserved_unknown"}
            or not _validate_money(cost.get("primer_cost_usd"), nullable=True)
            or not _validate_money(cost.get("measured_cost_usd"), nullable=True)
            or not _validate_money(
                cost.get("observed_cost_usd"),
                nullable=True,
            )
            or not _validate_money(
                cost.get("known_observed_cost_usd"),
                nullable=False,
            )
            or not _validate_money(
                cost.get("budget_debit_usd"),
                nullable=False,
            )
            or not _validate_money(cost.get("reservation_usd"), nullable=False)
        ):
            raise GatewayProbeRunError(
                "results row has malformed retry attempt cost"
            )
        known_total = sum(
            (
                Decimal(item)
                for item in (
                    cost.get("primer_cost_usd"),
                    cost.get("measured_cost_usd"),
                )
                if item is not None
            ),
            Decimal(0),
        )
        if (
            Decimal(cost["known_observed_cost_usd"]) != known_total
            or (
                cost["cost_status"] == "observed"
                and (
                    cost["observed_cost_usd"] is None
                    or Decimal(cost["observed_cost_usd"]) != known_total
                    or Decimal(cost["budget_debit_usd"]) != known_total
                )
            )
            or (
                cost["cost_status"] == "reserved_unknown"
                and (
                    cost["observed_cost_usd"] is not None
                    or Decimal(cost["budget_debit_usd"])
                    != Decimal(cost["reservation_usd"])
                    or Decimal(cost["budget_debit_usd"]) < known_total
                )
            )
        ):
            raise GatewayProbeRunError(
                "results row retry attempt cost is inconsistent"
            )
        expected_reservation = (
            Decimal(retry["max_input_tokens"] or 0)
            * Decimal(retry["reservation_input_per_million_usd"])
            + Decimal(retry["max_output_tokens"])
            * Decimal(retry["reservation_output_per_million_usd"])
        ) / Decimal(1_000_000)
        if condition == "warm":
            expected_reservation *= 2
        if Decimal(cost["reservation_usd"]) != expected_reservation:
            raise GatewayProbeRunError(
                "results row retry reservation is inconsistent"
            )
        normalized_attempts.append((attempt_outcome, decision, cost))
    if retry.get("first_attempt_outcome") != attempts[0]["outcome"]:
        raise GatewayProbeRunError(
            "results row first attempt outcome is inconsistent"
        )
    if retry.get("eventual_outcome") != attempts[-1]["outcome"]:
        raise GatewayProbeRunError(
            "results row eventual outcome is inconsistent"
        )
    if retry.get("recovered") is not (
        attempt_count > 1 and attempts[-1]["outcome"]["success"] is True
    ):
        raise GatewayProbeRunError("results row recovery evidence is inconsistent")
    recovery_timing = _exact_mapping(
        retry.get("recovery_timing"),
        {
            "initial_request_to_final_response_headers_s",
            "initial_request_to_final_semantic_output_s",
            "initial_request_to_completion_s",
            "final_attempt_request_start_offset_s",
        },
        "recovery timing",
    )
    if any(not _duration(item) for item in recovery_timing.values()):
        raise GatewayProbeRunError("results row has malformed recovery timing")


def validate_row_shape(row: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version", "benchmark", "cell_id", "identity",
        "expected_arm_ids", "scheduled_blocks_per_condition", "arm_role",
        "baseline", "model_match", "outcome", "route_integrity",
        "request_metrics", "reuse_evidence", "billing", "retry_evidence",
    }
    if set(row) != expected_fields:
        raise GatewayProbeRunError("results row does not match schema v3")
    if row.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise GatewayProbeRunError("results row has unsupported schema_version")
    if row.get("benchmark") != BENCHMARK:
        raise GatewayProbeRunError("results row benchmark is malformed")
    identity = _exact_mapping(
        row.get("identity"),
        {"benchmark", "experiment", "arm", "case", "comparison", "schedule"},
        "identity",
    )
    if _exact_mapping(
        identity.get("benchmark"), {"name", "track"}, "benchmark identity"
    ) != {"name": BENCHMARK, "track": probe_spec.TRACK}:
        raise GatewayProbeRunError("results row benchmark identity is malformed")
    for name, fields in (
        ("experiment", {"id", "digest"}),
        ("arm", {"id", "digest"}),
        ("case", {"id", "prompt_digest"}),
        ("comparison", {"schedule_digest", "price_digest"}),
    ):
        value = _exact_mapping(identity.get(name), fields, f"{name} identity")
        if any(
            not _safe_identifier(item, nullable=False)
            for item in value.values()
        ):
            raise GatewayProbeRunError(f"results row {name} identity is malformed")
    schedule = _exact_mapping(
        identity.get("schedule"),
        {"condition", "repetition", "block_id", "block_attempt"},
        "schedule identity",
    )
    if (
        schedule.get("condition") not in {"cold", "warm"}
        or not isinstance(schedule.get("repetition"), int)
        or isinstance(schedule.get("repetition"), bool)
        or schedule.get("repetition") < 1
        or not _safe_identifier(schedule.get("block_id"), nullable=False)
        or not isinstance(schedule.get("block_attempt"), int)
        or isinstance(schedule.get("block_attempt"), bool)
        or schedule.get("block_attempt") < 0
    ):
        raise GatewayProbeRunError("results row schedule identity is malformed")
    try:
        expected_cell_id = cell_id(identity)
    except (TypeError, ValueError, gateway_spec.GatewaySpecError) as exc:
        raise GatewayProbeRunError(
            "results row identity is not canonical JSON"
        ) from exc
    if row.get("cell_id") != expected_cell_id:
        raise GatewayProbeRunError("results row cell_id does not match identity")
    expected_arms = row.get("expected_arm_ids")
    if (
        not isinstance(expected_arms, list)
        or not expected_arms
        or any(
            not _safe_identifier(arm, nullable=False) for arm in expected_arms
        )
        or expected_arms != sorted(set(expected_arms))
        or not isinstance(row.get("scheduled_blocks_per_condition"), int)
        or isinstance(row.get("scheduled_blocks_per_condition"), bool)
        or row.get("scheduled_blocks_per_condition") < 1
        or row.get("arm_role") not in {"direct", "gateway"}
        or not isinstance(row.get("baseline"), bool)
        or not _safe_identifier(row.get("model_match"), nullable=False)
    ):
        raise GatewayProbeRunError("results row top-level provenance is malformed")
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
        "stream_no_semantic_output",
    }
    budget_reasons = {
        None,
        "primer_cost_unavailable",
        "usd_cap_reached_by_primer",
        "measured_cost_unavailable",
        "usd_cap_reached",
    }
    if (
        outcome.get("error_class") not in error_classes
        or outcome.get("error_detail") not in error_details
        or outcome.get("budget_exhausted_reason") not in budget_reasons
    ):
        raise GatewayProbeRunError("results row outcome taxonomy is malformed")
    if (
        outcome.get("success") is not outcome.get("available")
        or (outcome.get("success") and not outcome.get("attempted"))
    ):
        raise GatewayProbeRunError("results row outcome is inconsistent")
    _validate_route_integrity(row.get("route_integrity"), "route integrity")
    route = row["route_integrity"]
    if route is None:
        raise GatewayProbeRunError("results row route integrity is malformed")
    request_metrics = _exact_mapping(
        row.get("request_metrics"),
        {
            "setup", "timing", "receipt_headers", "usage", "generation",
            "cache", "route", "costs", "stream", "coverage",
        },
        "request metrics",
    )
    condition = schedule["condition"]
    _validate_setup(
        request_metrics.get("setup"),
        "request setup",
        nullable=condition == "warm",
    )
    if condition == "warm" and request_metrics.get("setup") is not None:
        raise GatewayProbeRunError("warm request contains measured setup evidence")
    timing = _exact_mapping(
        request_metrics.get("timing"),
        {
            "request_to_response_headers_s",
            "request_to_first_body_byte_s",
            "request_to_semantic_ttft_s",
            "request_stream_total_s",
            "cold_end_to_end_response_headers_s",
            "cold_end_to_end_first_body_byte_s",
            "cold_end_to_end_semantic_ttft_s",
            "cold_end_to_end_stream_total_s",
        },
        "request timing",
    )
    if any(not _duration(value) for value in timing.values()):
        raise GatewayProbeRunError("results row has malformed request timing")
    _validate_timing_order(timing, condition=condition)
    _validate_receipts(request_metrics.get("receipt_headers"), "request receipts")
    _validate_usage(request_metrics.get("usage"), "request usage")
    _validate_generation(request_metrics.get("generation"), "request generation")
    _validate_cache(request_metrics.get("cache"), "request cache", nullable=False)
    _validate_route(request_metrics.get("route"), "request route")
    _validate_costs(request_metrics.get("costs"), "request costs")
    _validate_stream(request_metrics.get("stream"), "request stream")
    _validate_coverage(request_metrics.get("coverage"), "request coverage")
    _validate_outcome_evidence(
        outcome,
        timing,
        request_metrics.get("stream"),
    )
    _validate_retry_evidence(
        row.get("retry_evidence"),
        condition=condition,
    )
    eventual = row["retry_evidence"]["eventual_outcome"]
    if any(
        eventual[name] != outcome[name]
        for name in (
            "success",
            "timed_out",
            "error_class",
            "error_detail",
        )
    ):
        raise GatewayProbeRunError(
            "results row eventual outcome does not match cell outcome"
        )
    final_attempt = row["retry_evidence"]["attempts"][-1]
    if (
        final_attempt["phase"] == "measured"
        and eventual["http_status"] != outcome["http_status"]
    ):
        raise GatewayProbeRunError(
            "results row eventual status does not match cell outcome"
        )
    primer_fields = {
        "required", "completed", "http_status", "socket_reused",
        "primer_nonce_sha256", "measured_nonce_sha256", "setup",
        "receipt_headers", "route_integrity", "usage", "cache", "costs",
    }
    primer_value = row.get("reuse_evidence")
    if (
        not isinstance(primer_value, Mapping)
        or not primer_fields <= set(primer_value)
        or not set(primer_value) <= primer_fields | {"route", "stream"}
    ):
        raise GatewayProbeRunError("results row has malformed reuse evidence")
    primer = primer_value
    if (
        not isinstance(primer.get("required"), bool)
        or not isinstance(primer.get("completed"), bool)
        or primer.get("required") is not (condition == "warm")
        or (
            primer.get("http_status") is not None
            and (
                not isinstance(primer.get("http_status"), int)
                or isinstance(primer.get("http_status"), bool)
            )
        )
        or (
            primer.get("socket_reused") is not None
            and not isinstance(primer.get("socket_reused"), bool)
        )
    ):
        raise GatewayProbeRunError("results row reuse evidence is malformed")
    for name in ("primer_nonce_sha256", "measured_nonce_sha256"):
        value = primer.get(name)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise GatewayProbeRunError("results row reuse nonce is malformed")
    _validate_setup(primer.get("setup"), "primer setup", nullable=False)
    _validate_receipts(primer.get("receipt_headers"), "primer receipts")
    _validate_route_integrity(
        primer.get("route_integrity"), "primer route integrity"
    )
    if "route" in primer:
        _validate_route(primer.get("route"), "primer route")
    _validate_usage(primer.get("usage"), "primer usage")
    _validate_cache(primer.get("cache"), "primer cache", nullable=True)
    _validate_costs(primer.get("costs"), "primer costs")
    _validate_primer_stream(primer.get("stream"), "primer stream")
    primer_stream = primer.get("stream")
    if (
        "stream" in primer
        and primer.get("completed") is True
        and (
            not isinstance(primer_stream, Mapping)
            or primer_stream.get("done") is not True
            or primer_stream.get("finalized") is not True
            or primer_stream.get("terminal_status") not in {None, "completed"}
        )
    ):
        raise GatewayProbeRunError(
            "completed primer stream evidence is inconsistent"
        )
    if condition == "warm" and outcome.get("success") is True:
        primer_route = primer.get("route_integrity")
        primer_reasons = (
            primer_route.get("reasons")
            if isinstance(primer_route, Mapping)
            else None
        )
        recovered_primer = set(primer_reasons or ()) == {
            "multiple_attempts",
            "unsuccessful_attempt",
        }
        recovered_attempts_valid = (
            recovered_primer
            and _recovered_primer_attempts_valid(primer.get("route"))
        )
        if (
            primer.get("completed") is not True
            or primer.get("socket_reused") is not True
            or not isinstance(primer.get("http_status"), int)
            or not 200 <= primer["http_status"] < 300
            or not isinstance(primer_route, Mapping)
            or primer_route.get("status") != "verified"
            or (
                primer_reasons
                and not recovered_attempts_valid
            )
        ):
            raise GatewayProbeRunError(
                "successful warm row lacks verified reuse evidence"
            )
    billing = _exact_mapping(
        row.get("billing"),
        {
            "primer_cost_usd", "measured_cost_usd",
            "charged_cost_usd", "observed_cost_usd",
            "known_observed_cost_usd", "budget_debit_usd",
            "cost_status", "unknown_cost_attempts", "stop_required",
        },
        "billing evidence",
    )
    if (
        not isinstance(billing.get("stop_required"), bool)
        or billing.get("cost_status") not in {"observed", "reserved_unknown"}
        or not isinstance(billing.get("unknown_cost_attempts"), int)
        or isinstance(billing.get("unknown_cost_attempts"), bool)
        or billing.get("unknown_cost_attempts") < 0
        or not _validate_money(billing.get("primer_cost_usd"), nullable=True)
        or not _validate_money(billing.get("measured_cost_usd"), nullable=True)
        or not _validate_money(billing.get("charged_cost_usd"), nullable=True)
        or not _validate_money(billing.get("observed_cost_usd"), nullable=True)
        or not _validate_money(
            billing.get("known_observed_cost_usd"),
            nullable=False,
        )
        or not _validate_money(billing.get("budget_debit_usd"), nullable=False)
    ):
        raise GatewayProbeRunError("results row billing evidence is malformed")
    component_total = sum(
        (
            Decimal(value)
            for value in (
                billing.get("primer_cost_usd"),
                billing.get("measured_cost_usd"),
            )
            if value is not None
        ),
        Decimal(0),
    )
    if Decimal(billing["known_observed_cost_usd"]) != component_total:
        raise GatewayProbeRunError("results row charged cost is inconsistent")
    attempt_costs = row["retry_evidence"]["attempts"]
    unknown_attempts = sum(
        attempt["cost"]["cost_status"] == "reserved_unknown"
        for attempt in attempt_costs
    )
    known_attempt_total = sum(
        (
            Decimal(attempt["cost"]["known_observed_cost_usd"])
            for attempt in attempt_costs
        ),
        Decimal(0),
    )
    debit_total = sum(
        (
            Decimal(attempt["cost"]["budget_debit_usd"])
            for attempt in attempt_costs
        ),
        Decimal(0),
    )
    if (
        billing["unknown_cost_attempts"] != unknown_attempts
        or billing["cost_status"]
        != ("observed" if unknown_attempts == 0 else "reserved_unknown")
        or Decimal(billing["known_observed_cost_usd"]) != known_attempt_total
        or Decimal(billing["budget_debit_usd"]) != debit_total
        or (
            unknown_attempts == 0
            and (
                billing["observed_cost_usd"] is None
                or billing["charged_cost_usd"] is None
                or Decimal(billing["observed_cost_usd"]) != known_attempt_total
                or Decimal(billing["charged_cost_usd"]) != known_attempt_total
            )
        )
        or (
            unknown_attempts > 0
            and (
                billing["observed_cost_usd"] is not None
                or billing["charged_cost_usd"] is not None
            )
        )
    ):
        raise GatewayProbeRunError(
            "results row aggregate attempt cost is inconsistent"
        )


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
        validate_row_shape(row)
        retry_evidence = row["retry_evidence"]
        if (
            retry_evidence["max_total_attempts"]
            != experiment.budget.max_total_attempts
            or retry_evidence["max_input_tokens"]
            != experiment.budget.max_input_tokens
            or retry_evidence["max_output_tokens"]
            != experiment.budget.max_output_tokens
            or retry_evidence["retry_deadline_s"]
            != experiment.budget.retry_deadline_s
        ):
            raise GatewayProbeRunError(
                "results row retry policy does not match experiment"
            )
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
    validate_row_shape(row)
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
    raw = billing.get("budget_debit_usd")
    try:
        amount = Decimal(str(raw))
    except (ArithmeticError, ValueError) as exc:
        raise GatewayProbeRunError(
            "results row has malformed charged cost"
        ) from exc
    if not amount.is_finite() or amount < 0:
        raise GatewayProbeRunError("results row has malformed charged cost")
    return amount
