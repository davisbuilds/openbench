"""Matched, task-weighted reporting for schema-v2 Gateway Bench rows.

The report consumes canonical gateway identities from :mod:`obench.results` plus
these result fields:

* ``arm_role`` (``"direct"`` or ``"gateway"``) and boolean ``baseline``;
* ``result`` with ``solved``, ``checker_score``, ``available``, optional
  ``duration_s``, and optional ``infrastructure_invalid_reason``;
* ``route_integrity`` with boolean ``pass`` and a list of reason strings;
* ``proxy_metrics.calls``, where each call may contain ``timing`` (``ttfb_s``
  and ``semantic_ttft_s``), ``generation`` (paired ``output_tokens`` and
  ``duration_s``), ``tokens`` (input, output, and total), ``cache``
  (``cached_input_tokens`` and ``cache_write_input_tokens``), ``route``
  (``provider`` and ``served_model``), and ``costs``.

``costs`` maps a basis name to an object containing ``amount_usd``, ``currency``
and ``effective_at``. Missing conditional timing or cost evidence affects only
that metric's coverage. Invalid infrastructure, route-integrity, and incomplete
all-arm blocks are excluded as whole matched blocks and counted by reason.
Gateway/provider outcomes remain ordinary attempted cells.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
import json
import math
import random
from typing import Any

from obench import gateway_spec, results


DEFAULT_BOOTSTRAP_REPLICATES = 10_000
DEFAULT_BOOTSTRAP_SEED = 20_260_722
_COST_BASES = (
    "gateway_reported",
    "invoice_reconciled",
    "frozen_list_estimate",
)
_CALL_COVERAGE_FIELDS = {
    "ttfb_s": "ttfb",
    "semantic_ttft_s": "ttft",
    "mean_input_tokens_per_call": "input_tokens",
    "mean_output_tokens_per_call": "token_output_tokens",
    "mean_total_tokens_per_call": "total_tokens",
    "mean_cached_input_tokens_per_call": "cached_input_tokens",
    "mean_cache_write_input_tokens_per_call": "cache_write_input_tokens",
}
_ROLES = frozenset({"direct", "gateway"})


class GatewayReportError(ValueError):
    """Raised when gateway rows cannot support an unambiguous report."""


def _number(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise GatewayReportError(f"{name} must be a finite number >= {minimum}")
    return float(value)


def _optional_number(value: Any, name: str, *, minimum: float = 0.0) -> float | None:
    if value is None:
        return None
    return _number(value, name, minimum=minimum)


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GatewayReportError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise GatewayReportError(f"{name} must be a non-empty string")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise GatewayReportError(f"{name} must be a boolean")
    return value


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _interval(
    task_values: Mapping[str, float],
    *,
    replicates: int,
    seed: int,
) -> dict[str, float] | None:
    if not task_values:
        return None
    ordered = [task_values[task] for task in sorted(task_values)]
    rng = random.Random(seed)
    samples = []
    for _ in range(replicates):
        samples.append(sum(rng.choice(ordered) for _ in ordered) / len(ordered))
    samples.sort()
    return {
        "confidence": 0.95,
        "low": _percentile(samples, 0.025),
        "high": _percentile(samples, 0.975),
    }


def _ratio_interval(
    numerators: Mapping[str, float],
    denominators: Mapping[str, float],
    *,
    replicates: int,
    seed: int,
) -> dict[str, float] | None:
    tasks = sorted(set(numerators) & set(denominators))
    if not tasks:
        return None
    rng = random.Random(seed)
    samples = []
    for _ in range(replicates):
        selected = [rng.choice(tasks) for _ in tasks]
        denominator = sum(denominators[task] for task in selected)
        if denominator > 0:
            samples.append(sum(numerators[task] for task in selected) / denominator)
    if not samples:
        return None
    samples.sort()
    return {
        "confidence": 0.95,
        "low": _percentile(samples, 0.025),
        "high": _percentile(samples, 0.975),
    }


def _metric(
    task_values: Mapping[str, float],
    *,
    eligible_tasks: int,
    cells_covered: int,
    cells_total: int,
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    values = list(task_values.values())
    return {
        "estimate": _mean(values),
        "interval": _interval(task_values, replicates=replicates, seed=seed),
        "task_coverage": {
            "covered": len(task_values),
            "total": eligible_tasks,
            "ratio": len(task_values) / eligible_tasks if eligible_tasks else 0.0,
        },
        "cell_coverage": {
            "covered": cells_covered,
            "total": cells_total,
            "ratio": cells_covered / cells_total if cells_total else 0.0,
        },
    }


def _arm_metadata(
    row: Mapping[str, Any], row_number: int
) -> tuple[str, bool]:
    role = row.get("arm_role")
    baseline = row.get("baseline")
    if role is None and isinstance(row.get("arm"), Mapping):
        role = row["arm"].get("role")
        baseline = row["arm"].get("baseline", baseline)
    if baseline is None:
        baseline = row.get("is_baseline")
    role = _string(role, f"row {row_number} arm_role")
    if role not in _ROLES:
        raise GatewayReportError(
            f"row {row_number} arm_role must be one of {sorted(_ROLES)}"
        )
    return role, _bool(baseline, f"row {row_number} baseline")


def _stratum(identity: results.CellIdentity) -> tuple[Any, ...]:
    return (
        identity.policy_digest,
        identity.catalog_digest,
        identity.price_digest,
        identity.sampling_digest,
        identity.schedule_digest,
        identity.harness,
        identity.candidate,
        identity.harness_version,
        identity.execution_lane,
        identity.image_digest,
        identity.budget_timeout_s,
        identity.budget_max_calls,
        identity.budget_max_output_tokens,
        identity.budget_usd_cap,
        identity.adapter_timeout_s,
        identity.checker_timeout_s,
    )


def _block_key(identity: results.CellIdentity) -> tuple[Any, ...]:
    return (
        identity.task,
        identity.window_id,
        identity.repetition,
        identity.block_attempt,
    )


def _block_coordinate(identity: results.CellIdentity) -> tuple[Any, ...]:
    return identity.task, identity.window_id, identity.repetition


def _logical_cell_key(identity: results.CellIdentity) -> tuple[Any, ...]:
    return (*_block_key(identity), identity.arm_id)


def _infrastructure_reason(row: Mapping[str, Any], row_number: int) -> str | None:
    result = _object(row.get("result"), f"row {row_number} result")
    reason = result.get("infrastructure_invalid_reason")
    if reason is None:
        valid = result.get("infrastructure_valid", True)
        if not isinstance(valid, bool):
            raise GatewayReportError(
                f"row {row_number} result.infrastructure_valid must be a boolean"
            )
        return None if valid else "unspecified_infrastructure"
    return _string(reason, f"row {row_number} infrastructure_invalid_reason")


def _route_reasons(row: Mapping[str, Any], row_number: int) -> list[str]:
    integrity = _object(row.get("route_integrity"), f"row {row_number} route_integrity")
    passed = _bool(integrity.get("pass"), f"row {row_number} route_integrity.pass")
    raw_reasons = integrity.get("reasons", [])
    if not isinstance(raw_reasons, list) or not all(
        isinstance(reason, str) and reason for reason in raw_reasons
    ):
        raise GatewayReportError(
            f"row {row_number} route_integrity.reasons must be non-empty strings"
        )
    if passed and raw_reasons:
        raise GatewayReportError(
            f"row {row_number} route integrity passes but contains failure reasons"
        )
    if not passed and not raw_reasons:
        return ["unspecified"]
    return raw_reasons


def _costs(
    call: Mapping[str, Any], row_number: int, call_number: int
) -> dict[str, tuple[float, str, str]]:
    raw = call.get("costs", {})
    if raw is None:
        raw = {}
    raw = _object(raw, f"row {row_number} call {call_number} costs")
    unknown = sorted(set(raw) - set(_COST_BASES))
    if unknown:
        raise GatewayReportError(
            f"row {row_number} call {call_number} has unknown cost bases: "
            + ", ".join(unknown)
        )
    parsed = {}
    for basis, item in raw.items():
        item = _object(item, f"row {row_number} call {call_number} costs.{basis}")
        amount = _number(
            item.get("amount_usd"),
            f"row {row_number} call {call_number} costs.{basis}.amount_usd",
        )
        currency = _string(
            item.get("currency"),
            f"row {row_number} call {call_number} costs.{basis}.currency",
        )
        if currency != "USD":
            raise GatewayReportError(
                f"row {row_number} call {call_number} costs.{basis}.currency "
                "must be USD for amount_usd"
            )
        effective_at = _string(
            item.get("effective_at"),
            f"row {row_number} call {call_number} costs.{basis}.effective_at",
        )
        parsed[basis] = (amount, currency, effective_at)
    return parsed


def _route_label(provider: str | None, model: str | None) -> str:
    if provider is None or model is None:
        return provider or model or "unknown"
    prefix = provider + "/"
    if model.casefold().startswith(prefix.casefold()):
        return model
    return f"{provider}/{model}"


def _cell(row: Mapping[str, Any], row_number: int) -> dict[str, Any]:
    result = _object(row.get("result"), f"row {row_number} result")
    solved = _bool(result.get("solved"), f"row {row_number} result.solved")
    score = _number(
        result.get("checker_score"),
        f"row {row_number} result.checker_score",
    )
    max_calls_exhausted = result.get("budget_exhausted_reason") == "max_calls"
    if max_calls_exhausted:
        solved = False
        score = 0.0
    available = _bool(
        result.get("available"),
        f"row {row_number} result.available",
    )
    duration = _optional_number(
        result.get("duration_s"),
        f"row {row_number} result.duration_s",
    )
    timed_out = result.get("timed_out", False)
    if not isinstance(timed_out, bool):
        raise GatewayReportError(f"row {row_number} result.timed_out must be a boolean")
    proxy = _object(row.get("proxy_metrics"), f"row {row_number} proxy_metrics")
    raw_calls = proxy.get("calls")
    if not isinstance(raw_calls, list):
        raise GatewayReportError(f"row {row_number} proxy_metrics.calls must be a list")

    calls = []
    for call_number, raw_call in enumerate(raw_calls, 1):
        call = _object(raw_call, f"row {row_number} call {call_number}")
        timing_value = call.get("timing")
        timing = _object(
            {} if timing_value is None else timing_value,
            f"row {row_number} call {call_number} timing",
        )
        generation = call.get("generation")
        if generation is not None:
            generation = _object(
                generation,
                f"row {row_number} call {call_number} generation",
            )
        cache_value = call.get("cache")
        cache = _object(
            {} if cache_value is None else cache_value,
            f"row {row_number} call {call_number} cache",
        )
        tokens_value = call.get("tokens")
        tokens = _object(
            {} if tokens_value is None else tokens_value,
            f"row {row_number} call {call_number} tokens",
        )
        route = _object(
            call.get("route", {}),
            f"row {row_number} call {call_number} route",
        )
        provider = route.get("provider")
        model = route.get("served_model")
        raw_attempts = route.get("attempts", [])
        if not isinstance(raw_attempts, list) or not all(
            isinstance(attempt, Mapping) for attempt in raw_attempts
        ):
            raise GatewayReportError(
                f"row {row_number} call {call_number} route.attempts must be a list"
            )
        attempts_present = route.get("attempts_present", False)
        if not isinstance(attempts_present, bool):
            raise GatewayReportError(
                f"row {row_number} call {call_number} "
                "route.attempts_present must be a boolean"
            )
        if provider is not None:
            provider = _string(
                provider, f"row {row_number} call {call_number} route.provider"
            )
        if model is not None:
            model = _string(
                model, f"row {row_number} call {call_number} route.served_model"
            )
        fallback_observed = len(raw_attempts) > 1 or any(
            (
                isinstance(attempt.get("status"), int)
                and not isinstance(attempt.get("status"), bool)
                and not 200 <= attempt["status"] < 300
            )
            or (
                provider is not None
                and isinstance(attempt.get("provider"), str)
                and attempt["provider"].casefold() != provider.casefold()
            )
            or (
                model is not None
                and isinstance(attempt.get("model"), str)
                and attempt["model"] != model
            )
            for attempt in raw_attempts
        )
        output_tokens = generation_duration = None
        if generation is not None:
            output_tokens = _optional_number(
                generation.get("output_tokens"),
                f"row {row_number} call {call_number} generation.output_tokens",
            )
            generation_duration = _optional_number(
                generation.get("duration_s"),
                f"row {row_number} call {call_number} generation.duration_s",
            )
            if (output_tokens is None) != (generation_duration is None):
                raise GatewayReportError(
                    f"row {row_number} call {call_number} generation evidence "
                    "must pair output_tokens with duration_s"
                )
        calls.append(
            {
                "ttfb": _optional_number(
                    timing.get("ttfb_s"),
                    f"row {row_number} call {call_number} timing.ttfb_s",
                ),
                "ttft": _optional_number(
                    timing.get("semantic_ttft_s"),
                    f"row {row_number} call {call_number} timing.semantic_ttft_s",
                ),
                "output_tokens": output_tokens,
                "input_tokens": _optional_number(
                    tokens.get("input_tokens"),
                    f"row {row_number} call {call_number} tokens.input_tokens",
                ),
                "token_output_tokens": _optional_number(
                    tokens.get("output_tokens"),
                    f"row {row_number} call {call_number} tokens.output_tokens",
                ),
                "total_tokens": _optional_number(
                    tokens.get("total_tokens"),
                    f"row {row_number} call {call_number} tokens.total_tokens",
                ),
                "generation_duration": generation_duration,
                "cached_input_tokens": _optional_number(
                    cache.get("cached_input_tokens"),
                    f"row {row_number} call {call_number} "
                    "cache.cached_input_tokens",
                ),
                "cache_write_input_tokens": _optional_number(
                    cache.get("cache_write_input_tokens"),
                    f"row {row_number} call {call_number} "
                    "cache.cache_write_input_tokens",
                ),
                "route": _route_label(provider, model),
                "attempts": len(raw_attempts),
                "attempts_present": attempts_present,
                "fallback_observed": fallback_observed,
                "costs": _costs(call, row_number, call_number),
            }
        )

    identity = results.gateway_identity_from_row(row)
    timeout = float(identity.budget_timeout_s)
    return {
        "solved": 1.0 if solved else 0.0,
        "score": score,
        "availability": 1.0 if available else 0.0,
        "max_calls_exhausted": max_calls_exhausted,
        "latency": (
            min(duration, timeout)
            if duration is not None
            else timeout if timed_out else None
        ),
        "calls": calls,
    }


def _cell_call_metric(cell: Mapping[str, Any], name: str) -> float | None:
    values = [call[name] for call in cell["calls"] if call[name] is not None]
    return _mean(values)


def _cell_throughput(cell: Mapping[str, Any]) -> float | None:
    paired = [
        (call["output_tokens"], call["generation_duration"])
        for call in cell["calls"]
        if call["output_tokens"] is not None and call["generation_duration"] is not None
    ]
    if not paired:
        return None
    duration = sum(item[1] for item in paired)
    if duration <= 0:
        return None
    return sum(item[0] for item in paired) / duration


def _cell_cache_hit_rate(cell: Mapping[str, Any]) -> float | None:
    values = [
        call["cached_input_tokens"]
        for call in cell["calls"]
        if call["cached_input_tokens"] is not None
    ]
    if not values:
        return None
    return sum(value > 0 for value in values) / len(values)


def _cell_cached_input_fraction(cell: Mapping[str, Any]) -> float | None:
    paired = [
        (call["cached_input_tokens"], call["input_tokens"])
        for call in cell["calls"]
        if call["cached_input_tokens"] is not None
        and call["input_tokens"] is not None
    ]
    if not paired:
        return None
    total_input = sum(item[1] for item in paired)
    if total_input <= 0:
        return None
    return sum(item[0] for item in paired) / total_input


def _cell_cost(cell: Mapping[str, Any], basis: str) -> float | None:
    calls = cell["calls"]
    if not calls or any(basis not in call["costs"] for call in calls):
        return None
    return sum(call["costs"][basis][0] for call in calls)


def _task_values(
    cells_by_task: Mapping[str, Sequence[Mapping[str, Any]]],
    getter: Any,
) -> tuple[dict[str, float], int]:
    values = {}
    covered_cells = 0
    for task, cells in cells_by_task.items():
        cell_values = [getter(cell) for cell in cells]
        covered = [value for value in cell_values if value is not None]
        covered_cells += len(covered)
        if covered:
            values[task] = sum(covered) / len(covered)
    return values, covered_cells


def _seed(base: int, label: str) -> int:
    value = base & ((1 << 64) - 1)
    for byte in label.encode("utf-8"):
        value = ((value * 1_000_003) ^ byte) & ((1 << 64) - 1)
    return value


def aggregate(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_arm_ids: Iterable[str] | None = None,
    bootstrap_replicates: int = DEFAULT_BOOTSTRAP_REPLICATES,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Build a JSON-safe Gateway Bench report DTO from schema-v2 gateway rows."""
    if (
        not isinstance(bootstrap_replicates, int)
        or isinstance(bootstrap_replicates, bool)
        or bootstrap_replicates < 1
    ):
        raise GatewayReportError("bootstrap_replicates must be a positive integer")
    if not isinstance(bootstrap_seed, int) or isinstance(bootstrap_seed, bool):
        raise GatewayReportError("bootstrap_seed must be an integer")

    materialized = list(rows)
    if not materialized:
        raise GatewayReportError("at least one gateway row is required")

    parsed = []
    experiment_ids = set()
    experiment_digests = set()
    tracks = set()
    strata = set()
    cell_ids = {}
    logical_cells = {}
    arm_metadata: dict[str, tuple[str, bool, str]] = {}
    model_matches = set()
    provider_prompt_modes = set()
    task_metadata: dict[str, tuple[str, str, str]] = {}
    block_coordinates: dict[str, tuple[Any, ...]] = {}
    coordinate_block_ids: dict[tuple[Any, ...], str] = {}
    for row_number, row in enumerate(materialized, 1):
        if not isinstance(row, Mapping):
            raise GatewayReportError(f"row {row_number} must be an object")
        try:
            identity = results.gateway_identity_from_row(row)
            cell_id = results.result_cell_id(row)
        except results.ResultError as exc:
            raise GatewayReportError(f"row {row_number} has invalid identity: {exc}") from exc
        tracks.add(identity.track)
        model_match = row.get("model_match", "exact_revision")
        if model_match not in {"exact_revision", "model_family", "rolling_alias"}:
            raise GatewayReportError(
                f"row {row_number} has invalid model_match"
            )
        model_matches.add(model_match)
        provider_prompt_mode = row.get("provider_prompt_mode")
        if provider_prompt_mode not in gateway_spec.PROVIDER_PROMPT_MODES:
            raise GatewayReportError(
                f"row {row_number} has invalid provider_prompt_mode"
            )
        if provider_prompt_mode != identity.provider_prompt_mode:
            raise GatewayReportError(
                f"row {row_number} provider_prompt_mode conflicts with identity"
            )
        provider_prompt_modes.add(provider_prompt_mode)
        experiment_ids.add(identity.experiment_id)
        experiment_digests.add(identity.experiment_digest)
        strata.add(_stratum(identity))
        if cell_id in cell_ids:
            raise GatewayReportError(
                f"duplicate cell_id {cell_id!r} on rows {cell_ids[cell_id]} and {row_number}"
            )
        cell_ids[cell_id] = row_number
        logical = _logical_cell_key(identity)
        if logical in logical_cells:
            raise GatewayReportError(
                "duplicate logical cell on rows "
                f"{logical_cells[logical]} and {row_number}"
            )
        logical_cells[logical] = row_number
        role, baseline = _arm_metadata(row, row_number)
        metadata = (role, baseline, identity.arm_digest)
        previous = arm_metadata.setdefault(identity.arm_id, metadata)
        if previous != metadata:
            raise GatewayReportError(f"arm {identity.arm_id!r} metadata is inconsistent")
        task_provenance = (
            identity.task_digest,
            identity.checker_digest,
            identity.workspace_source_sha,
        )
        previous_task = task_metadata.setdefault(identity.task, task_provenance)
        if previous_task != task_provenance:
            raise GatewayReportError(f"task {identity.task!r} provenance is inconsistent")
        coordinates = (
            identity.task,
            identity.window_id,
            identity.repetition,
            identity.block_attempt,
        )
        previous_coordinates = block_coordinates.setdefault(identity.block_id, coordinates)
        if previous_coordinates != coordinates:
            raise GatewayReportError(
                f"block_id {identity.block_id!r} maps to mixed schedule coordinates"
            )
        coordinate_key = _block_key(identity)
        previous_block_id = coordinate_block_ids.setdefault(
            coordinate_key, identity.block_id
        )
        if previous_block_id != identity.block_id:
            raise GatewayReportError(
                "one logical block maps to multiple block_id values"
            )
        infrastructure_reason = _infrastructure_reason(row, row_number)
        route_reasons = _route_reasons(row, row_number)
        parsed.append(
            {
                "identity": identity,
                "row": row,
                "row_number": row_number,
                "infrastructure_reason": infrastructure_reason,
                "route_reasons": route_reasons,
            }
        )

    if len(experiment_ids) != 1 or len(experiment_digests) != 1:
        raise GatewayReportError("rows mix experiments")
    if tracks != {gateway_spec.TRACK}:
        raise GatewayReportError(
            f"rows must use the {gateway_spec.TRACK!r} gateway track"
        )
    track = next(iter(tracks))
    if len(model_matches) != 1:
        raise GatewayReportError("rows mix model_match policies")
    model_match = next(iter(model_matches))
    if len(provider_prompt_modes) != 1:
        raise GatewayReportError("rows mix provider cache modes")
    provider_prompt_mode = next(iter(provider_prompt_modes))
    if len(strata) != 1:
        raise GatewayReportError("rows mix comparison strata")

    baseline_arms = [
        arm_id
        for arm_id, (_role, baseline, _digest) in arm_metadata.items()
        if baseline
    ]
    if len(baseline_arms) != 1:
        raise GatewayReportError(f"{track} requires exactly one baseline arm")
    baseline_arm = baseline_arms[0]
    if arm_metadata[baseline_arm][0] != "direct":
        raise GatewayReportError("the gateway baseline arm must be direct")
    if any(
        role == "direct" and arm_id != baseline_arm
        for arm_id, (role, _baseline, _digest) in arm_metadata.items()
    ):
        raise GatewayReportError("Gateway Bench allows exactly one direct arm")
    if not any(
        role == "gateway"
        for role, _baseline, _digest in arm_metadata.values()
    ):
        raise GatewayReportError("Gateway Bench requires at least one gateway arm")

    if expected_arm_ids is None:
        expected_arms = frozenset(arm_metadata)
    else:
        expected_list = list(expected_arm_ids)
        if not expected_list or not all(
            isinstance(arm_id, str) and arm_id for arm_id in expected_list
        ):
            raise GatewayReportError("expected_arm_ids must contain non-empty strings")
        if len(set(expected_list)) != len(expected_list):
            raise GatewayReportError("expected_arm_ids contains duplicates")
        expected_arms = frozenset(expected_list)
        unknown = sorted(expected_arms - set(arm_metadata))
        extra = sorted(set(arm_metadata) - expected_arms)
        if unknown or extra:
            raise GatewayReportError(
                "observed arms do not match expected_arm_ids"
                + (f"; missing: {', '.join(unknown)}" if unknown else "")
                + (f"; unexpected: {', '.join(extra)}" if extra else "")
            )

    latest_attempts: dict[tuple[Any, ...], int] = {}
    for item in parsed:
        identity = item["identity"]
        coordinate = _block_coordinate(identity)
        latest_attempts[coordinate] = max(
            latest_attempts.get(coordinate, -1), identity.block_attempt
        )

    blocks: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in parsed:
        identity = item["identity"]
        coordinate = _block_coordinate(identity)
        if identity.block_attempt == latest_attempts[coordinate]:
            blocks[coordinate].append(item)

    exclusions = Counter()
    included_blocks = []
    for block_key in sorted(blocks):
        block = blocks[block_key]
        observed = {item["identity"].arm_id for item in block}
        if observed != expected_arms or len(block) != len(expected_arms):
            exclusions["incomplete_all_arm_block"] += 1
            continue
        reasons = {
            item["infrastructure_reason"]
            for item in block
            if item["infrastructure_reason"] is not None
        }
        if reasons:
            for reason in sorted(reasons):
                exclusions[f"infrastructure:{reason}"] += 1
            continue
        route_reasons = {
            reason
            for item in block
            for reason in item["route_reasons"]
        }
        if route_reasons:
            for reason in sorted(route_reasons):
                exclusions[f"route_integrity:{reason}"] += 1
            continue
        included_blocks.append(block)

    arm_cells: dict[str, dict[str, list[dict[str, Any]]]] = {
        arm_id: defaultdict(list) for arm_id in sorted(expected_arms)
    }
    analyzed_blocks = []
    for block in included_blocks:
        analyzed = {"task": block[0]["identity"].task, "cells": {}}
        for item in block:
            arm_id = item["identity"].arm_id
            cell = _cell(item["row"], item["row_number"])
            arm_cells[arm_id][item["identity"].task].append(cell)
            analyzed["cells"][arm_id] = cell
        analyzed_blocks.append(analyzed)

    task_names = sorted(
        {item["identity"].task for block in included_blocks for item in block}
    )
    eligible_tasks = len(task_names)
    report_arms = {}
    for arm_id in sorted(expected_arms):
        cells_by_task = arm_cells[arm_id]
        cells_total = sum(len(cells) for cells in cells_by_task.values())
        getters = {
            "solve_rate": lambda cell: cell["solved"],
            "mean_checker_score": lambda cell: cell["score"],
            "availability": lambda cell: cell["availability"],
            "latency_s": lambda cell: cell["latency"],
            "ttfb_s": lambda cell: _cell_call_metric(cell, "ttfb"),
            "semantic_ttft_s": lambda cell: _cell_call_metric(cell, "ttft"),
            "throughput_tokens_per_s": _cell_throughput,
            "mean_input_tokens_per_call": lambda cell: _cell_call_metric(
                cell, "input_tokens"
            ),
            "mean_output_tokens_per_call": lambda cell: _cell_call_metric(
                cell, "token_output_tokens"
            ),
            "mean_total_tokens_per_call": lambda cell: _cell_call_metric(
                cell, "total_tokens"
            ),
            "cache_hit_call_rate": _cell_cache_hit_rate,
            "cached_input_fraction": _cell_cached_input_fraction,
            "mean_cached_input_tokens_per_call": lambda cell: _cell_call_metric(
                cell, "cached_input_tokens"
            ),
            "mean_cache_write_input_tokens_per_call": lambda cell: _cell_call_metric(
                cell, "cache_write_input_tokens"
            ),
        }
        metrics = {}
        for name, getter in getters.items():
            values, covered_cells = _task_values(cells_by_task, getter)
            metrics[name] = _metric(
                values,
                eligible_tasks=eligible_tasks,
                cells_covered=covered_cells,
                cells_total=cells_total,
                replicates=bootstrap_replicates,
                seed=_seed(bootstrap_seed, f"arm:{arm_id}:{name}"),
            )
            if name in _CALL_COVERAGE_FIELDS or name == "throughput_tokens_per_s":
                call_total = sum(
                    len(cell["calls"])
                    for cells in cells_by_task.values()
                    for cell in cells
                )
                if name == "throughput_tokens_per_s":
                    covered_calls = sum(
                        1
                        for cells in cells_by_task.values()
                        for cell in cells
                        for call in cell["calls"]
                        if call["output_tokens"] is not None
                        and call["generation_duration"] is not None
                        and call["generation_duration"] > 0
                    )
                else:
                    call_name = _CALL_COVERAGE_FIELDS[name]
                    covered_calls = sum(
                        1
                        for cells in cells_by_task.values()
                        for cell in cells
                        for call in cell["calls"]
                        if call[call_name] is not None
                    )
                metrics[name]["call_coverage"] = {
                    "covered": covered_calls,
                    "total": call_total,
                    "ratio": covered_calls / call_total if call_total else 0.0,
                }

        costs = {}
        for basis in _COST_BASES:
            values, covered_cells = _task_values(
                cells_by_task, lambda cell, basis=basis: _cell_cost(cell, basis)
            )
            call_total = sum(
                len(cell["calls"])
                for cells in cells_by_task.values()
                for cell in cells
            )
            covered_calls = sum(
                1
                for cells in cells_by_task.values()
                for cell in cells
                for call in cell["calls"]
                if basis in call["costs"]
            )
            currencies = sorted(
                {
                    call["costs"][basis][1]
                    for cells in cells_by_task.values()
                    for cell in cells
                    for call in cell["calls"]
                    if basis in call["costs"]
                }
            )
            effective_at = sorted(
                {
                    call["costs"][basis][2]
                    for cells in cells_by_task.values()
                    for cell in cells
                    for call in cell["calls"]
                    if basis in call["costs"]
                }
            )
            attempted = _metric(
                values,
                eligible_tasks=eligible_tasks,
                cells_covered=covered_cells,
                cells_total=cells_total,
                replicates=bootstrap_replicates,
                seed=_seed(bootstrap_seed, f"arm:{arm_id}:cost:{basis}"),
            )
            solve_rate = metrics["solve_rate"]["estimate"]
            complete_coverage = (
                cells_total > 0
                and covered_cells == cells_total
                and covered_calls == call_total
            )
            solve_values, _covered_solve_cells = _task_values(
                cells_by_task, lambda cell: cell["solved"]
            )
            cost_per_solve = (
                attempted["estimate"] / solve_rate
                if complete_coverage
                and attempted["estimate"] is not None
                and solve_rate is not None
                and solve_rate > 0
                else None
            )
            costs[basis] = {
                "attempted_cost_usd": attempted,
                "cost_per_solve_usd": cost_per_solve,
                "cost_per_solve_interval": (
                    _ratio_interval(
                        values,
                        solve_values,
                        replicates=bootstrap_replicates,
                        seed=_seed(
                            bootstrap_seed,
                            f"arm:{arm_id}:cost_per_solve:{basis}",
                        ),
                    )
                    if complete_coverage
                    else None
                ),
                "basis_coverage": {
                    "covered_calls": covered_calls,
                    "total_calls": call_total,
                    "ratio": covered_calls / call_total if call_total else 0.0,
                    "covered_cells": covered_cells,
                    "total_cells": cells_total,
                    "complete": complete_coverage,
                },
                "currencies": currencies,
                "effective_at": effective_at,
            }

        task_routes: dict[str, Counter[str]] = defaultdict(Counter)
        for task, cells in cells_by_task.items():
            for cell in cells:
                task_routes[task].update(call["route"] for call in cell["calls"])
        route_names = sorted({route for counts in task_routes.values() for route in counts})
        distribution = {}
        for route in route_names:
            values = {
                task: counts[route] / sum(counts.values())
                for task, counts in task_routes.items()
                if counts
            }
            distribution[route] = {
                "share": _mean(list(values.values())),
                "interval": _interval(
                    values,
                    replicates=bootstrap_replicates,
                    seed=_seed(bootstrap_seed, f"arm:{arm_id}:route:{route}"),
                ),
                "task_coverage": {
                    "covered": len(values),
                    "total": eligible_tasks,
                    "ratio": len(values) / eligible_tasks if eligible_tasks else 0.0,
                },
            }

        max_calls_cells = sum(
            cell["max_calls_exhausted"]
            for cells in cells_by_task.values()
            for cell in cells
        )
        report_arm = {
            "role": arm_metadata[arm_id][0],
            "baseline": arm_metadata[arm_id][1],
            "arm_digest": arm_metadata[arm_id][2],
            "attempted_cells": cells_total,
            "max_calls": {
                "cells": max_calls_cells,
                "total_cells": cells_total,
                "ratio": max_calls_cells / cells_total if cells_total else 0.0,
            },
            "metrics": metrics,
            "costs": costs,
            "route_distribution": distribution,
        }
        report_arms[arm_id] = report_arm

    contrasts = {}
    contrast_getters = {
        "solve_rate": lambda cell: cell["solved"],
        "mean_checker_score": lambda cell: cell["score"],
        "availability": lambda cell: cell["availability"],
        "latency_s": lambda cell: cell["latency"],
        "ttfb_s": lambda cell: _cell_call_metric(cell, "ttfb"),
        "semantic_ttft_s": lambda cell: _cell_call_metric(cell, "ttft"),
        "throughput_tokens_per_s": _cell_throughput,
        "mean_input_tokens_per_call": lambda cell: _cell_call_metric(
            cell, "input_tokens"
        ),
        "mean_output_tokens_per_call": lambda cell: _cell_call_metric(
            cell, "token_output_tokens"
        ),
        "mean_total_tokens_per_call": lambda cell: _cell_call_metric(
            cell, "total_tokens"
        ),
        "cache_hit_call_rate": _cell_cache_hit_rate,
        "cached_input_fraction": _cell_cached_input_fraction,
        "mean_cached_input_tokens_per_call": lambda cell: _cell_call_metric(
            cell, "cached_input_tokens"
        ),
        "mean_cache_write_input_tokens_per_call": lambda cell: _cell_call_metric(
            cell, "cache_write_input_tokens"
        ),
        **{
            f"cost:{basis}": (lambda cell, basis=basis: _cell_cost(cell, basis))
            for basis in _COST_BASES
        },
    }
    for arm_id in sorted(expected_arms):
        if arm_id == baseline_arm:
            continue
        metrics = {}
        for name, getter in contrast_getters.items():
            block_differences: dict[str, list[float]] = defaultdict(list)
            covered_blocks = 0
            for block in analyzed_blocks:
                treatment = getter(block["cells"][arm_id])
                control = getter(block["cells"][baseline_arm])
                if treatment is not None and control is not None:
                    block_differences[block["task"]].append(treatment - control)
                    covered_blocks += 1
            paired = {
                task: sum(values) / len(values)
                for task, values in sorted(block_differences.items())
            }
            metrics[name] = {
                "estimate": _mean(list(paired.values())),
                "interval": _interval(
                    paired,
                    replicates=bootstrap_replicates,
                    seed=_seed(bootstrap_seed, f"contrast:{arm_id}:{name}"),
                ),
                "paired_task_coverage": {
                    "covered": len(paired),
                    "total": eligible_tasks,
                    "ratio": len(paired) / eligible_tasks if eligible_tasks else 0.0,
                },
                "paired_block_coverage": {
                    "covered": covered_blocks,
                    "total": len(analyzed_blocks),
                    "ratio": (
                        covered_blocks / len(analyzed_blocks)
                        if analyzed_blocks
                        else 0.0
                    ),
                },
            }
        contrasts[arm_id] = {
            "gateway_arm": arm_id,
            "direct_arm": baseline_arm,
            "direction": "gateway_minus_direct",
            "metrics": metrics,
        }

    max_calls_affected_blocks = sum(
        any(cell["max_calls_exhausted"] for cell in block["cells"].values())
        for block in analyzed_blocks
    )
    dto = {
        "schema_version": 1,
        "benchmark": results.GATEWAY_BENCHMARK,
        "track": track,
        "model_match": model_match,
        "provider_prompt_mode": provider_prompt_mode,
        "experiment_id": next(iter(experiment_ids)),
        "experiment_digest": next(iter(experiment_digests)),
        "budget": {
            "max_calls": parsed[0]["identity"].budget_max_calls,
        },
        "analysis": {
            "weighting": "calls_to_cell_complete_blocks_to_task_tasks_equal",
            "interval": "task_cluster_bootstrap_percentile",
            "bootstrap_seed": bootstrap_seed,
            "bootstrap_replicates": bootstrap_replicates,
            "wilson_intervals": False,
            "composite_score": False,
        },
        "blocks": {
            "observed": len(blocks),
            "included": len(included_blocks),
            "excluded": len(blocks) - len(included_blocks),
            "excluded_by_reason": dict(sorted(exclusions.items())),
            "max_calls_affected": max_calls_affected_blocks,
            "max_calls_rate": (
                max_calls_affected_blocks / len(included_blocks)
                if included_blocks
                else 0.0
            ),
        },
        "tasks": {"included": eligible_tasks, "names": task_names},
        "baseline_arm": baseline_arm,
        "arms": report_arms,
        "paired_contrasts": contrasts,
    }
    json.dumps(dto, allow_nan=False, sort_keys=True)
    return dto


def render_text(report: Mapping[str, Any]) -> str:
    """Render a compact human-readable summary of an aggregate report DTO."""
    blocks = report["blocks"]
    lines = [
        f"Gateway Bench: {report['track']} ({report['experiment_digest'][:12]})",
        f"Provider prompt mode: {report['provider_prompt_mode']}",
        (
            f"Blocks: {blocks['included']}/{blocks['observed']} included; "
            f"tasks: {report['tasks']['included']}"
        ),
    ]
    if blocks["excluded_by_reason"]:
        reasons = ", ".join(
            f"{reason}={count}"
            for reason, count in blocks["excluded_by_reason"].items()
        )
        lines.append(f"Excluded: {reasons}")
    for arm_id, arm in report["arms"].items():
        metrics = arm["metrics"]
        solve = metrics["solve_rate"]["estimate"]
        score = metrics["mean_checker_score"]["estimate"]
        availability = metrics["availability"]["estimate"]
        latency = metrics["latency_s"]["estimate"]
        role = arm["role"]
        lines.append(
            f"{arm_id} ({role}): solve {_format_percent(solve)}, "
            f"score {_format_number(score)}, availability {_format_percent(availability)}, "
            f"latency {_format_seconds(latency)}, "
            f"call cap {arm['max_calls']['cells']}/{arm['max_calls']['total_cells']} "
            f"({_format_percent(arm['max_calls']['ratio'])})"
        )
        for basis, cost in arm["costs"].items():
            attempted = cost["attempted_cost_usd"]["estimate"]
            if attempted is None and cost["basis_coverage"]["covered_calls"] == 0:
                continue
            lines.append(
                f"  {basis}: attempted ${_format_number(attempted, 4)}, "
                f"cost/solve {_format_cost(cost['cost_per_solve_usd'])}, "
                f"coverage {_format_percent(cost['basis_coverage']['ratio'])}"
            )
    for arm_id, contrast in report["paired_contrasts"].items():
        latency = contrast["metrics"]["latency_s"]["estimate"]
        solve = contrast["metrics"]["solve_rate"]["estimate"]
        control = contrast.get("fixed_control_arm", contrast.get("direct_arm"))
        lines.append(
            f"{arm_id} - {control} ({contrast['direction']}): "
            f"solve {_format_signed(solve)}, latency {_format_signed(latency, 's')}"
        )
    return "\n".join(lines)


def _format_number(value: float | None, places: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def _format_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _format_seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}s"


def _format_cost(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _format_signed(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:+.3f}{suffix}"


build_report = aggregate
generate_report = aggregate
