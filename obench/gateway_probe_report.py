"""Coverage-explicit reporting for request-level Gateway Probe rows."""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import gateway_probe_results, stats


class GatewayProbeReportError(ValueError):
    """Raised when probe rows cannot support an unambiguous report."""


_METRICS = {
    "dns_s": ("request_metrics", "connection", "dns_s"),
    "tcp_s": ("request_metrics", "connection", "tcp_s"),
    "tls_s": ("request_metrics", "connection", "tls_s"),
    "request_to_first_byte_s": ("request_metrics", "timing", "ttfb_s"),
    "semantic_ttft_s": ("request_metrics", "timing", "semantic_ttft_s"),
    "total_s": ("request_metrics", "timing", "total_s"),
    "throughput_tokens_per_s": (
        "request_metrics", "generation", "tokens_per_second",
    ),
    "input_tokens": ("request_metrics", "usage", "input_tokens"),
    "output_tokens": ("request_metrics", "usage", "output_tokens"),
    "total_tokens": ("request_metrics", "usage", "total_tokens"),
    "cached_input_tokens": (
        "request_metrics", "cache", "cached_input_tokens",
    ),
    "cache_write_input_tokens": (
        "request_metrics", "cache", "cache_write_input_tokens",
    ),
    "measured_cost_usd": (
        "request_metrics", "costs", "frozen_list_estimate", "amount_usd",
    ),
    "charged_cost_usd": ("billing", "charged_cost_usd"),
}


def _number(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


def _metric(row: Mapping[str, Any], path: Sequence[str]) -> float | None:
    value: Any = row
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return _number(value)


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _summary(values: Sequence[float], total: int) -> dict[str, Any]:
    return {
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "coverage": {
            "covered": len(values),
            "total": total,
            "ratio": len(values) / total if total else 0.0,
        },
    }


def _bootstrap_median(
    values: Sequence[float],
    *,
    seed: int,
    replicates: int,
) -> dict[str, Any] | None:
    if not values:
        return None
    rng = random.Random(seed)
    samples = []
    for _ in range(replicates):
        draw = [rng.choice(values) for _ in values]
        samples.append(_percentile(draw, 0.5))
    return {
        "confidence": 0.95,
        "low": _percentile(samples, 0.025),
        "high": _percentile(samples, 0.975),
        "replicates": replicates,
    }


def _seed(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:8], "big")


def aggregate(
    rows: Iterable[Mapping[str, Any]],
    *,
    bootstrap_replicates: int = 2_000,
) -> dict[str, Any]:
    materialized = list(rows)
    if not materialized:
        raise GatewayProbeReportError("at least one Gateway Probe row is required")
    if bootstrap_replicates < 1:
        raise GatewayProbeReportError("bootstrap_replicates must be positive")
    experiment_identities = set()
    comparison_digests = set()
    for row in materialized:
        if (
            not isinstance(row, Mapping)
            or row.get("schema_version") != gateway_probe_results.RESULT_SCHEMA_VERSION
            or row.get("benchmark") != gateway_probe_results.BENCHMARK
        ):
            raise GatewayProbeReportError("results contain an invalid probe row")
        identity = row.get("identity")
        if not isinstance(identity, Mapping):
            raise GatewayProbeReportError("result identity is missing")
        try:
            expected_cell_id = gateway_probe_results.cell_id(identity)
        except (TypeError, ValueError) as exc:
            raise GatewayProbeReportError("result identity is not canonical JSON") from exc
        if row.get("cell_id") != expected_cell_id:
            raise GatewayProbeReportError("result cell_id does not match identity")
        benchmark = identity.get("benchmark")
        experiment = identity.get("experiment")
        comparison = identity.get("comparison")
        if benchmark != {
            "name": gateway_probe_results.BENCHMARK,
            "track": "request_probe",
        }:
            raise GatewayProbeReportError("rows contain invalid benchmark provenance")
        if not isinstance(experiment, Mapping):
            raise GatewayProbeReportError("rows omit experiment provenance")
        experiment_identity = (experiment.get("id"), experiment.get("digest"))
        if not all(isinstance(item, str) and item for item in experiment_identity):
            raise GatewayProbeReportError("rows omit experiment provenance")
        experiment_identities.add(experiment_identity)
        if not isinstance(comparison, Mapping):
            raise GatewayProbeReportError("rows omit comparison digests")
        comparison_pair = (
            comparison.get("schedule_digest"),
            comparison.get("price_digest"),
        )
        if not all(isinstance(item, str) and item for item in comparison_pair):
            raise GatewayProbeReportError("rows omit comparison digests")
        comparison_digests.add(comparison_pair)
    if len(experiment_identities) != 1:
        raise GatewayProbeReportError("rows mix experiment provenance")
    if len(comparison_digests) != 1:
        raise GatewayProbeReportError("rows mix schedule or price digests")
    experiment_id, experiment_digest = next(iter(experiment_identities))
    schedule_digest, price_digest = next(iter(comparison_digests))
    expected_sets = {
        tuple(row.get("expected_arm_ids", ())) for row in materialized
    }
    if len(expected_sets) != 1 or not next(iter(expected_sets)):
        raise GatewayProbeReportError("rows mix or omit expected_arm_ids")
    expected_arms = next(iter(expected_sets))
    if (
        any(not isinstance(arm, str) or not arm for arm in expected_arms)
        or tuple(sorted(set(expected_arms))) != expected_arms
    ):
        raise GatewayProbeReportError("expected_arm_ids must be sorted and unique")
    scheduled_values = {
        row.get("scheduled_blocks_per_condition") for row in materialized
    }
    if len(scheduled_values) != 1:
        raise GatewayProbeReportError("rows mix scheduled block counts")
    scheduled_blocks = next(iter(scheduled_values))
    if not isinstance(scheduled_blocks, int) or scheduled_blocks < 1:
        raise GatewayProbeReportError("scheduled block count is invalid")

    attempts: dict[tuple[str, str, int], dict[int, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    arm_metadata = {}
    case_provenance = {}
    block_provenance = {}
    logical_rows = set()
    for row in materialized:
        identity = row.get("identity")
        arm_identity = identity.get("arm")
        case_identity = identity.get("case")
        schedule = identity.get("schedule")
        if not all(
            isinstance(item, Mapping)
            for item in (arm_identity, case_identity, schedule)
        ):
            raise GatewayProbeReportError("result provenance is malformed")
        arm = arm_identity.get("id")
        arm_digest = arm_identity.get("digest")
        case = case_identity.get("id")
        prompt_digest = case_identity.get("prompt_digest")
        condition = schedule.get("condition")
        repetition = schedule.get("repetition")
        attempt = schedule.get("block_attempt")
        block_id = schedule.get("block_id")
        if (
            arm not in expected_arms
            or not isinstance(arm_digest, str)
            or condition not in {"cold", "warm"}
            or not isinstance(case, str)
            or not isinstance(prompt_digest, str)
            or not isinstance(repetition, int)
            or isinstance(repetition, bool)
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 0
            or not isinstance(block_id, str)
        ):
            raise GatewayProbeReportError("result has invalid schedule identity")
        logical_key = (case, condition, repetition, attempt, arm)
        if logical_key in logical_rows:
            raise GatewayProbeReportError("results contain a duplicate logical arm row")
        logical_rows.add(logical_key)
        metadata = (
            arm_digest,
            row.get("arm_role"),
            row.get("baseline"),
            row.get("model_match"),
        )
        if (
            metadata[1] not in {"direct", "gateway"}
            or not isinstance(metadata[2], bool)
            or not isinstance(metadata[3], str)
        ):
            raise GatewayProbeReportError("result arm metadata is malformed")
        previous_metadata = arm_metadata.get(arm)
        if previous_metadata is not None and previous_metadata != metadata:
            raise GatewayProbeReportError("rows contain inconsistent arm provenance")
        arm_metadata[arm] = metadata
        previous_prompt_digest = case_provenance.get(case)
        if (
            previous_prompt_digest is not None
            and previous_prompt_digest != prompt_digest
        ):
            raise GatewayProbeReportError("rows contain inconsistent case provenance")
        case_provenance[case] = prompt_digest
        block_key = (case, condition, repetition, attempt)
        previous_block_id = block_provenance.get(block_key)
        if previous_block_id is not None and previous_block_id != block_id:
            raise GatewayProbeReportError("rows contain inconsistent block provenance")
        block_provenance[block_key] = block_id
        attempts[(case, condition, repetition)][attempt].append(row)
    missing_arm_metadata = sorted(set(expected_arms) - set(arm_metadata))
    if missing_arm_metadata:
        raise GatewayProbeReportError(
            "rows omit metadata for expected arms: " + ", ".join(missing_arm_metadata)
        )
    baselines = [arm for arm, item in arm_metadata.items() if item[2] is True]
    if len(baselines) != 1:
        raise GatewayProbeReportError("exactly one baseline arm is required")
    baseline = baselines[0]

    latest_rows = []
    complete_blocks: dict[tuple[str, str, int], dict[str, Mapping[str, Any]]] = {}
    complete_by_condition = defaultdict(int)
    for coordinate, by_attempt in attempts.items():
        latest = max(by_attempt)
        rows_for_attempt = by_attempt[latest]
        by_arm = {row["identity"]["arm"]["id"]: row for row in rows_for_attempt}
        latest_rows.extend(rows_for_attempt)
        if set(by_arm) == set(expected_arms):
            complete_blocks[coordinate] = by_arm
            complete_by_condition[coordinate[1]] += 1

    arms: dict[str, Any] = {}
    for condition in ("cold", "warm"):
        for arm in expected_arms:
            selected = [
                row for row in latest_rows
                if row["identity"]["arm"]["id"] == arm
                and row["identity"]["schedule"]["condition"] == condition
            ]
            attempted = sum(row.get("outcome", {}).get("attempted") is True for row in selected)
            success = sum(row.get("outcome", {}).get("success") is True for row in selected)
            route_counts = {
                status: sum(
                    row.get("route_integrity", {}).get("status") == status
                    for row in selected
                )
                for status in ("verified", "unverifiable", "failed")
            }
            availability_low, availability_high = stats.wilson_ci(success, attempted)
            arm_report = arms.setdefault(arm, {
                "role": arm_metadata[arm][1],
                "baseline": arm_metadata[arm][2],
                "conditions": {},
            })
            arm_report["conditions"][condition] = {
                "denominators": {
                    "scheduled": scheduled_blocks,
                    "attempted": attempted,
                    "success": success,
                    "request_failed": attempted - success,
                    "route_verified": route_counts["verified"],
                    "route_unverifiable": route_counts["unverifiable"],
                    "route_failed": route_counts["failed"],
                },
                "availability": {
                    "successes": success,
                    "attempted": attempted,
                    "rate": success / attempted if attempted else None,
                    "wilson95": {
                        "confidence": 0.95,
                        "low": availability_low,
                        "high": availability_high,
                    },
                },
                "metrics": {
                    name: _summary(
                        [
                            value for row in selected
                            if row.get("outcome", {}).get("success") is True
                            and (value := _metric(row, path)) is not None
                        ],
                        success,
                    )
                    for name, path in _METRICS.items()
                },
            }

    contrasts = {}
    for arm in expected_arms:
        if arm == baseline:
            continue
        arm_contrasts = {}
        for condition in ("cold", "warm"):
            metrics = {}
            condition_blocks = [
                rows_by_arm
                for coordinate, rows_by_arm in complete_blocks.items()
                if coordinate[1] == condition
            ]
            for name, path in _METRICS.items():
                deltas = []
                for rows_by_arm in condition_blocks:
                    direct = rows_by_arm[baseline]
                    gateway = rows_by_arm[arm]
                    if not all(
                        row.get("outcome", {}).get("success") is True
                        and row.get("route_integrity", {}).get("status") == "verified"
                        for row in (direct, gateway)
                    ):
                        continue
                    direct_value = _metric(direct, path)
                    gateway_value = _metric(gateway, path)
                    if direct_value is not None and gateway_value is not None:
                        deltas.append(gateway_value - direct_value)
                label = f"{experiment_digest}:{arm}:{condition}:{name}"
                metrics[name] = {
                    "median_gateway_minus_direct": _percentile(deltas, 0.5),
                    "interval": _bootstrap_median(
                        deltas,
                        seed=_seed(label),
                        replicates=bootstrap_replicates,
                    ),
                    "coverage": {
                        "covered": len(deltas),
                        "total": len(condition_blocks),
                        "ratio": len(deltas) / len(condition_blocks)
                        if condition_blocks else 0.0,
                    },
                }
            arm_contrasts[condition] = metrics
        contrasts[arm] = arm_contrasts

    minimum_blocks = min(complete_by_condition.get("cold", 0), complete_by_condition.get("warm", 0))
    return {
        "schema_version": 1,
        "benchmark": gateway_probe_results.BENCHMARK,
        "experiment_id": experiment_id,
        "experiment_digest": experiment_digest,
        "schedule_digest": schedule_digest,
        "price_digest": price_digest,
        "label": "exploratory" if minimum_blocks < 100 else "confirmatory",
        "complete_blocks": {
            condition: complete_by_condition.get(condition, 0)
            for condition in ("cold", "warm")
        },
        "scheduled_blocks_per_condition": scheduled_blocks,
        "baseline_arm_id": baseline,
        "arms": arms,
        "paired_contrasts": contrasts,
    }


def render_text(report: Mapping[str, Any]) -> str:
    def metric(item: Mapping[str, Any], name: str, statistic: str = "p50") -> Any:
        return item.get("metrics", {}).get(name, {}).get(statistic)

    def seconds(value: Any, *, signed: bool = False) -> str:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            return "n/a"
        prefix = "+" if signed and value > 0 else ""
        return f"{prefix}{value:.3f}s"

    def number(value: Any) -> str:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            return "n/a"
        return f"{value:.1f}"

    def coverage(value: Mapping[str, Any] | None) -> str:
        if not isinstance(value, Mapping):
            return "n/a"
        covered = value.get("covered")
        total = value.get("total")
        if not isinstance(covered, int) or not isinstance(total, int):
            return "n/a"
        return f"{covered}/{total}"

    def availability(value: Mapping[str, Any]) -> str:
        successes = value.get("successes")
        attempted = value.get("attempted")
        rate = value.get("rate")
        interval = value.get("wilson95")
        if not isinstance(attempted, int) or attempted == 0:
            return "n/a"
        if not isinstance(successes, int) or not isinstance(rate, (int, float)):
            return "n/a"
        if not isinstance(interval, Mapping):
            return f"{successes}/{attempted} ({rate:.1%})"
        low = interval.get("low")
        high = interval.get("high")
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            return f"{successes}/{attempted} ({rate:.1%})"
        return (
            f"{successes}/{attempted} ({rate:.1%}; "
            f"95% CI {low:.1%}-{high:.1%})"
        )

    def count_ratio(numerator: Any, denominator: Any) -> str:
        if (
            not isinstance(numerator, int)
            or not isinstance(denominator, int)
            or denominator == 0
        ):
            return "n/a"
        return f"{numerator}/{denominator}"

    def cost(item: Mapping[str, Any]) -> str:
        summary = item.get("metrics", {}).get("measured_cost_usd", {})
        value = summary.get("p50") if isinstance(summary, Mapping) else None
        cov = summary.get("coverage") if isinstance(summary, Mapping) else None
        rendered = f"${value:.6f}" if isinstance(value, (int, float)) else "n/a"
        return f"{rendered} ({coverage(cov)})"

    def delta(
        condition_metrics: Mapping[str, Any],
        name: str,
    ) -> str:
        value = condition_metrics.get(name, {}).get("median_gateway_minus_direct")
        return seconds(value, signed=True)

    def pair_coverage(condition_metrics: Mapping[str, Any]) -> str:
        names = (
            ("TTFT", "semantic_ttft_s"),
            ("TTFB", "request_to_first_byte_s"),
            ("Total", "total_s"),
        )
        rendered = [
            (label, coverage(condition_metrics.get(name, {}).get("coverage")))
            for label, name in names
        ]
        values = {value for _label, value in rendered}
        if len(values) == 1:
            return rendered[0][1]
        return ", ".join(f"{label} {value}" for label, value in rendered)

    lines = [
        f"Gateway Probe ({report['label']})",
        (
            "blocks "
            f"cold={report['complete_blocks']['cold']}/"
            f"{report['scheduled_blocks_per_condition']} "
            f"warm={report['complete_blocks']['warm']}/"
            f"{report['scheduled_blocks_per_condition']}"
        ),
        "",
        (
            "| Arm | Condition | Success / availability | Route verified | "
            "Semantic TTFT p50 / p95 | Total p50 | TTFB p50 | "
            "Tokens p50 | Measured cost p50 (coverage) |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, arm_report in report["arms"].items():
        for condition, item in arm_report["conditions"].items():
            den = item["denominators"]
            lines.append(
                f"| {arm} | {condition} | {availability(item['availability'])} | "
                f"{count_ratio(den['route_verified'], den['attempted'])} | "
                f"{seconds(metric(item, 'semantic_ttft_s'))} / "
                f"{seconds(metric(item, 'semantic_ttft_s', 'p95'))} | "
                f"{seconds(metric(item, 'total_s'))} | "
                f"{seconds(metric(item, 'request_to_first_byte_s'))} | "
                f"{number(metric(item, 'total_tokens'))} | {cost(item)} |"
            )
    lines.extend([
        "",
        (
            "| Gateway | Condition | Median delta TTFT | Median delta TTFB | "
            "Median delta total | Pair coverage |"
        ),
        "|---|---|---:|---:|---:|---:|",
    ])
    for arm, conditions in report["paired_contrasts"].items():
        for condition, condition_metrics in conditions.items():
            lines.append(
                f"| {arm} | {condition} | "
                f"{delta(condition_metrics, 'semantic_ttft_s')} | "
                f"{delta(condition_metrics, 'request_to_first_byte_s')} | "
                f"{delta(condition_metrics, 'total_s')} | "
                f"{pair_coverage(condition_metrics)} |"
            )
    return "\n".join(lines)
