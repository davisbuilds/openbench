"""Schedule and orchestrate request-level Gateway Probe experiments."""

from __future__ import annotations

import dataclasses
import json
import os
import random
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import gateway_run, gateway_spec
from . import gateway_probe_http as probe_http
from . import gateway_probe_results as probe_results
from . import gateway_probe_spec as probe_spec
from .gateway_probe_models import GatewayProbeRunError, ProbeBlock, RunSummary


FROZEN_PRICES_ENV = gateway_run.FROZEN_PRICES_ENV
_COST_UNAVAILABLE_STOP_REASONS = frozenset({
    "primer_cost_unavailable",
    "measured_cost_unavailable",
})


def build_schedule(
    experiment: probe_spec.GatewayProbeExperiment,
) -> tuple[ProbeBlock, ...]:
    """Interleave cold and warm matched blocks deterministically."""
    rng = random.Random(experiment.schedule_seed)
    by_condition: dict[str, list[tuple[probe_spec.ProbeCase, int]]] = {}
    for condition in probe_spec.CONDITIONS:
        coordinates = [
            (case, repetition)
            for repetition in range(1, experiment.repetitions + 1)
            for case in experiment.cases
        ]
        rng.shuffle(coordinates)
        by_condition[condition] = coordinates
    first = rng.randrange(2)
    blocks = []
    arm_ids = [arm.arm_id for arm in experiment.arms]
    total_per_condition = len(by_condition["cold"])
    for index in range(total_per_condition * 2):
        condition = probe_spec.CONDITIONS[(index + first) % 2]
        case, repetition = by_condition[condition].pop()
        order = list(arm_ids)
        rotation = len(blocks) % len(order)
        order = order[rotation:] + order[:rotation]
        if (len(blocks) // len(order)) % 2:
            order.reverse()
        blocks.append(ProbeBlock(
            case.case_id,
            case.prompt_digest,
            condition,
            repetition,
            tuple(order),
        ))
    return tuple(blocks)


def validate_experiment(
    path: str | os.PathLike[str],
) -> probe_spec.GatewayProbeExperiment:
    return probe_spec.load_experiment(path)


def doctor_experiment(
    path: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    experiment = validate_experiment(path)
    env = dict(os.environ if environ is None else environ)
    required_auth = sorted({arm.auth_env for arm in experiment.arms})
    missing_auth = [name for name in required_auth if not env.get(name)]
    prices, _snapshot = gateway_run.load_frozen_prices(env)
    missing_prices = sorted(
        {arm.canonical_model for arm in experiment.arms} - set(prices)
    )
    return {
        "ok": not missing_auth and not missing_prices,
        "experiment_id": experiment.experiment_id,
        "experiment_digest": experiment.digest,
        "arms": len(experiment.arms),
        "cases": len(experiment.cases),
        "blocks_per_condition": len(experiment.cases) * experiment.repetitions,
        "missing_auth_envs": missing_auth,
        "missing_price_models": missing_prices,
        "live_requests": False,
    }


def _validate_historical_cost_unavailable_recovery(
    stopped_rows: list[dict[str, Any]],
    *,
    by_coordinate: Mapping[
        tuple[str, str, int], Mapping[int, list[dict[str, Any]]]
    ],
    expected_arm_ids: set[str],
    spent: Decimal,
    usd_cap: Decimal,
) -> None:
    if spent >= usd_cap:
        raise GatewayProbeRunError(
            "cost-unavailable block recovery requires known charged spend "
            "below budget.usd_cap"
        )
    for row in stopped_rows:
        reason = row["outcome"]["budget_exhausted_reason"]
        if reason not in _COST_UNAVAILABLE_STOP_REASONS:
            raise GatewayProbeRunError(
                "cost-unavailable block recovery cannot bypass stop reason "
                f"{reason!r}"
            )
        identity = row["identity"]
        schedule_item = identity["schedule"]
        coordinate = (
            identity["case"]["id"],
            schedule_item["condition"],
            schedule_item["repetition"],
        )
        attempts = by_coordinate[coordinate]
        latest = max(attempts)
        latest_arm_ids = {
            item["identity"]["arm"]["id"] for item in attempts[latest]
        }
        if (
            schedule_item["block_attempt"] != latest
            or latest_arm_ids != expected_arm_ids
        ):
            raise GatewayProbeRunError(
                "cost-unavailable block recovery requires every stop to belong "
                "to the latest complete expected-arm block attempt"
            )


def _budget_stop_reason(row: Mapping[str, Any]) -> str | None:
    billing = row.get("billing")
    outcome = row.get("outcome")
    if not isinstance(billing, Mapping) or not isinstance(outcome, Mapping):
        raise GatewayProbeRunError("results row budget stop evidence is malformed")
    stop_required = billing.get("stop_required")
    reason = outcome.get("budget_exhausted_reason")
    if (stop_required is True) != (reason is not None):
        raise GatewayProbeRunError(
            "results row budget stop evidence is inconsistent"
        )
    return reason


def run_experiment(
    experiment_path: str | os.PathLike[str],
    *,
    results_path: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
    force: bool = False,
    allow_cost_unavailable_block_recovery: bool = False,
) -> RunSummary:
    if force and allow_cost_unavailable_block_recovery:
        raise GatewayProbeRunError(
            "cost-unavailable block recovery cannot be combined with force"
        )
    experiment = validate_experiment(experiment_path)
    env = dict(os.environ if environ is None else environ)
    prices, price_snapshot = gateway_run.load_frozen_prices(env)
    missing_prices = sorted(
        {arm.canonical_model for arm in experiment.arms} - set(prices)
    )
    if missing_prices:
        raise GatewayProbeRunError(
            "missing frozen prices for: " + ", ".join(missing_prices)
        )
    plans, secrets = probe_spec.compile_route_plans(
        experiment,
        environ=env,
        admitted_auth_envs={arm.auth_env for arm in experiment.arms},
    )
    plans_by_arm = {plan.arm_id: plan for plan in plans}
    arms_by_id = {arm.arm_id: arm for arm in experiment.arms}
    cases_by_id = {case.case_id: case for case in experiment.cases}
    schedule = build_schedule(experiment)
    schedule_digest = gateway_spec.canonical_digest(
        [dataclasses.asdict(block) for block in schedule]
    )
    price_digest = gateway_spec.canonical_digest(price_snapshot)
    path = Path(results_path)
    rows = probe_results.read_rows(path)
    probe_results.validate_resume_rows(
        rows,
        experiment=experiment,
        schedule=schedule,
        schedule_digest=schedule_digest,
        price_digest=price_digest,
    )
    by_coordinate: dict[
        tuple[str, str, int], dict[int, list[dict[str, Any]]]
    ] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        identity = row["identity"]
        schedule_item = identity["schedule"]
        coordinate = (
            identity["case"]["id"],
            schedule_item["condition"],
            schedule_item["repetition"],
        )
        by_coordinate[coordinate][schedule_item["block_attempt"]].append(row)
    usd_cap = Decimal(experiment.budget.usd_cap)
    spent = sum((probe_results.charged_cost(row) for row in rows), Decimal(0))
    stopped_rows = [
        row for row in rows if _budget_stop_reason(row) is not None
    ]
    stop_for_budget = spent >= usd_cap or bool(stopped_rows)
    appended = completed = replaced = skipped = 0
    expected = {arm.arm_id for arm in experiment.arms}
    if allow_cost_unavailable_block_recovery and stopped_rows:
        _validate_historical_cost_unavailable_recovery(
            stopped_rows,
            by_coordinate=by_coordinate,
            expected_arm_ids=expected,
            spent=spent,
            usd_cap=usd_cap,
        )
        stop_for_budget = False
    scheduled_per_condition = len(experiment.cases) * experiment.repetitions
    for block in schedule:
        if stop_for_budget:
            break
        attempts = by_coordinate.get(block.coordinate, {})
        latest = max(attempts) if attempts else -1
        latest_arms = {
            row["identity"]["arm"]["id"] for row in attempts.get(latest, [])
        }
        if latest >= 0 and latest_arms == expected and not force:
            skipped += 1
            continue
        attempt = latest + 1
        if latest >= 0:
            replaced += 1
        block_completed = True
        stop_after_block = False
        for arm_id in block.arm_ids:
            arm = arms_by_id[arm_id]
            identity = probe_results.make_identity(
                experiment,
                arm,
                block,
                attempt,
                schedule_digest,
                price_digest,
            )
            result = probe_http.execute_request(
                experiment=experiment,
                case=cases_by_id[block.case_id],
                block=block,
                plan=plans_by_arm[arm_id],
                secret=secrets.value_for(arm_id),
                prices=prices,
                remaining_usd_cap=usd_cap - spent,
            )
            row = {
                "schema_version": probe_results.RESULT_SCHEMA_VERSION,
                "benchmark": probe_results.BENCHMARK,
                "cell_id": probe_results.cell_id(identity),
                "identity": identity,
                "expected_arm_ids": sorted(expected),
                "scheduled_blocks_per_condition": scheduled_per_condition,
                "arm_role": arm.route_kind,
                "baseline": arm.baseline,
                "model_match": experiment.model_match,
                **result,
            }
            stop_reason = _budget_stop_reason(row)
            serialized = json.dumps(row, sort_keys=True)
            for forbidden in (
                cases_by_id[block.case_id].prompt,
                probe_http.nonce(experiment.digest, block, "primer"),
                probe_http.nonce(experiment.digest, block, "measured"),
                secrets.value_for(arm_id),
            ):
                if forbidden and forbidden in serialized:
                    raise GatewayProbeRunError(
                        "private probe payload leaked into result row"
                    )
            probe_results.append_row(path, row)
            appended += 1
            spent += probe_results.charged_cost(result)
            stop_required = stop_reason is not None
            if stop_required or spent >= usd_cap:
                # Recovery is bounded to the rest of this matched block and
                # never overrides a known cap hit.
                if (
                    allow_cost_unavailable_block_recovery
                    and stop_required
                    and stop_reason in _COST_UNAVAILABLE_STOP_REASONS
                    and spent < usd_cap
                ):
                    stop_after_block = True
                    continue
                stop_for_budget = True
                block_completed = False
                break
        if not block_completed:
            break
        completed += 1
        if stop_after_block:
            stop_for_budget = True
            break
    return RunSummary(path, appended, completed, replaced, skipped)
