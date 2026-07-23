"""Gateway Tax experiment runner.

Every routed cell gets a fresh task workspace, a sanitized adapter subprocess,
and a lifecycle-managed proxy ledger.  The ledger is sealed before the checker
runs and before the result row is serialized.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from . import proxy, results, router_spec
from .adapters import pi
from .publish import task_content_digest
from .run import read_instruction, run_checker
from .workspace import WorkspaceError, materialize_workspace


CHECKER_TIMEOUT_S = 120
FROZEN_PRICES_ENV = "OPENBENCH_ROUTER_FROZEN_PRICES_JSON"
ADAPTERS_DIR_ENV = "OPENBENCH_ROUTER_ADAPTERS_DIR"
LOCAL_LANE = "local-exploratory-route-isolation"
DOCKER_LANE = "docker-exploratory-route-isolation"
_RESULT_SENTINEL = "__BENCH_RESULT__"
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()
_POLICY = {
    "track": "gateway_tax",
    "fallback": False,
    "retries": 0,
    "cache": False,
    "checker_authority": True,
    "replacement_unit": "complete_all_arm_block_attempt",
}


class RouterRunError(RuntimeError):
    """Raised when an experiment cannot be run without ambiguous evidence."""


@dataclass(frozen=True, slots=True)
class TaskProvenance:
    name: str
    path: Path
    task_digest: str
    checker_digest: str
    workspace_source_sha: str


@dataclass(frozen=True, slots=True)
class ScheduleBlock:
    task: str
    window_id: str
    repetition: int
    arm_ids: tuple[str, ...]

    @property
    def coordinate(self) -> tuple[str, str, int]:
        return self.task, self.window_id, self.repetition


@dataclass(frozen=True, slots=True)
class Price:
    input_per_million: Decimal
    output_per_million: Decimal
    effective_at: str


@dataclass(frozen=True, slots=True)
class RunSummary:
    results_path: Path
    rows_appended: int
    blocks_completed: int
    blocks_replaced: int
    blocks_skipped: int


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest_file(path: Path) -> str:
    return _digest_bytes(path.read_bytes())


def _tree_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RouterRunError(f"workspace contains a symlink: {relative}")
        if path.is_dir():
            continue
        encoded = relative.encode("utf-8")
        data = path.read_bytes()
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


def _task_path(tasks_dir: str | os.PathLike[str], task: str) -> Path:
    root = Path(tasks_dir).resolve()
    path = (root / task).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RouterRunError(f"task escapes --tasks-dir: {task!r}") from exc
    if not path.is_dir():
        raise RouterRunError(f"task directory does not exist: {path}")
    if (path / "DROPPED.md").exists():
        raise RouterRunError(f"task {task!r} is dropped")
    for name in ("instruction.md", "checker.sh"):
        if not (path / name).is_file():
            raise RouterRunError(f"task {task!r} is missing {name}")
    return path


def inspect_tasks(
    experiment: router_spec.RouterExperiment,
    tasks_dir: str | os.PathLike[str],
) -> dict[str, TaskProvenance]:
    """Validate task inputs and freeze provenance before scheduling."""
    inspected = {}
    for task in experiment.tasks:
        path = _task_path(tasks_dir, task)
        with tempfile.TemporaryDirectory(prefix="obench_router_probe_") as temp:
            try:
                materialize_workspace(str(path), temp)
            except WorkspaceError as exc:
                raise RouterRunError(
                    f"task {task!r} workspace materialization failed: {exc}"
                ) from exc
            workspace_sha = _tree_digest(Path(temp))
        inspected[task] = TaskProvenance(
            name=task,
            path=path,
            task_digest=task_content_digest(str(path)),
            checker_digest=_digest_file(path / "checker.sh"),
            workspace_source_sha=workspace_sha,
        )
    return inspected


def build_schedule(experiment: router_spec.RouterExperiment) -> tuple[ScheduleBlock, ...]:
    """Return deterministic complete blocks with cyclic arm counterbalancing."""
    arm_ids = [arm.arm_id for arm in experiment.arms]
    rng = random.Random(experiment.schedule_seed)
    rng.shuffle(arm_ids)
    coordinates = [
        (task, window.window_id, repetition)
        for window in experiment.windows
        for repetition in range(1, experiment.repetitions_per_window + 1)
        for task in experiment.tasks
    ]
    blocks = []
    for index, (task, window_id, repetition) in enumerate(coordinates):
        rotation = index % len(arm_ids)
        order = arm_ids[rotation:] + arm_ids[:rotation]
        if (index // len(arm_ids)) % 2:
            order = list(reversed(order))
        blocks.append(ScheduleBlock(task, window_id, repetition, tuple(order)))
    return tuple(blocks)


def _active_schedule(
    experiment: router_spec.RouterExperiment,
    schedule: Sequence[ScheduleBlock],
    now: datetime | None = None,
) -> tuple[ScheduleBlock, ...]:
    now = now or datetime.now(timezone.utc)
    active = {
        window.window_id
        for window in experiment.windows
        if (
            datetime.fromisoformat(window.start.replace("Z", "+00:00"))
            <= now
            < datetime.fromisoformat(window.end.replace("Z", "+00:00"))
        )
    }
    return tuple(block for block in schedule if block.window_id in active)


def _load_prices(environ: Mapping[str, str]) -> tuple[dict[str, Price], dict[str, Any]]:
    raw = environ.get(FROZEN_PRICES_ENV)
    if not raw:
        return {}, {"status": "unavailable", "source": FROZEN_PRICES_ENV}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RouterRunError(f"{FROZEN_PRICES_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise RouterRunError(f"{FROZEN_PRICES_ENV} must be a non-empty JSON object")
    prices = {}
    canonical = []
    for key, item in sorted(decoded.items()):
        if not isinstance(key, str) or "/" not in key or not isinstance(item, dict):
            raise RouterRunError(
                f"{FROZEN_PRICES_ENV} entries must be provider/model objects"
            )
        try:
            input_rate = Decimal(str(item["input_per_million"]))
            output_rate = Decimal(str(item["output_per_million"]))
            effective_at = item["effective_at"]
        except (KeyError, InvalidOperation) as exc:
            raise RouterRunError(f"invalid frozen price for {key!r}") from exc
        if (
            not input_rate.is_finite()
            or not output_rate.is_finite()
            or input_rate < 0
            or output_rate < 0
            or not isinstance(effective_at, str)
            or not effective_at
        ):
            raise RouterRunError(f"invalid frozen price for {key!r}")
        prices[key] = Price(input_rate, output_rate, effective_at)
        canonical.append({
            "model": key,
            "input_per_million": str(input_rate),
            "output_per_million": str(output_rate),
            "effective_at": effective_at,
            "currency": "USD",
        })
    return prices, {
        "schema_version": 1,
        "price_id": "frozen-env-v1",
        "currency": "USD",
        "prices": canonical,
    }


def load_frozen_prices(
    environ: Mapping[str, str],
) -> tuple[dict[str, Price], dict[str, Any]]:
    return _load_prices(environ)


def _missing_price_models(
    experiment: router_spec.RouterExperiment,
    prices: Mapping[str, Price],
) -> list[str]:
    required = {arm.canonical_model for arm in experiment.arms}
    return sorted(required - set(prices))


def _price_snapshot_path(results_path: Path) -> Path:
    return results_path.parent / f".{results_path.stem}.router-prices.json"


def persist_price_snapshot(results_path: Path, snapshot: Mapping[str, Any]) -> Path:
    path = _price_snapshot_path(results_path)
    raw = results.canonical_json(snapshot) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != raw:
            raise RouterRunError("persisted frozen price snapshot does not match this run")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return path


def load_persisted_price_snapshot(results_path: Path) -> dict[str, Any]:
    path = _price_snapshot_path(results_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RouterRunError(f"cannot load persisted frozen prices from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RouterRunError(f"persisted frozen prices in {path} are malformed")
    return value


def policy_snapshot() -> dict[str, Any]:
    return dict(_POLICY)


def catalog_snapshot(experiment: router_spec.RouterExperiment) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "catalog_id": experiment.digest,
        "name": "frozen-router-arm-catalog",
        "entries": [
            {
                "model": arm.canonical_model,
                "model_id": arm.requested_model,
                "provider": arm.requested_provider,
            }
            for arm in experiment.arms
        ],
    }


def _comparison_digests(
    experiment: router_spec.RouterExperiment,
    schedule: Sequence[ScheduleBlock],
    price_snapshot: Mapping[str, Any],
) -> dict[str, str]:
    return {
        "policy_digest": results.canonical_digest(policy_snapshot()),
        "catalog_digest": results.canonical_digest(catalog_snapshot(experiment)),
        "price_digest": results.canonical_digest(price_snapshot),
        "sampling_digest": results.canonical_digest(
            {
                arm.arm_id: arm.sampling.to_dict()
                for arm in experiment.arms
            }
        ),
        "schedule_digest": results.canonical_digest(
            [dataclasses.asdict(block) for block in schedule]
        ),
    }


def _lane(exec_mode: str) -> str:
    return LOCAL_LANE if exec_mode == "local" else DOCKER_LANE


def _identity(
    *,
    experiment: router_spec.RouterExperiment,
    arm: router_spec.Arm,
    task: TaskProvenance,
    block: ScheduleBlock,
    attempt: int,
    digests: Mapping[str, str],
    harness_version: str,
    exec_mode: str,
    image_digest: str | None,
) -> results.CellIdentity:
    block_data = {
        "experiment": experiment.digest,
        "task": block.task,
        "window": block.window_id,
        "repetition": block.repetition,
        "attempt": attempt,
    }
    block_id = "router-block-" + results.canonical_digest(block_data)
    return results.CellIdentity.for_router(
        track=experiment.track,
        experiment_id=experiment.experiment_id,
        experiment_digest=experiment.digest,
        arm_id=arm.arm_id,
        arm_digest=arm.digest,
        task=task.name,
        task_digest=task.task_digest,
        checker_digest=task.checker_digest,
        workspace_source_sha=task.workspace_source_sha,
        harness=experiment.harness,
        candidate=None,
        harness_version=harness_version,
        execution_lane=_lane(exec_mode),
        image_digest=image_digest,
        budget_timeout_s=experiment.budget.timeout_s,
        budget_max_calls=experiment.budget.max_calls,
        budget_max_output_tokens=experiment.budget.max_output_tokens,
        budget_usd_cap=experiment.budget.usd_cap,
        adapter_timeout_s=experiment.budget.timeout_s,
        checker_timeout_s=CHECKER_TIMEOUT_S,
        window_id=block.window_id,
        repetition=block.repetition,
        block_id=block_id,
        block_attempt=attempt,
        **digests,
    )


def _existing_attempts(
    state: results.ResumeState,
    experiment: router_spec.RouterExperiment,
    expected_arms: set[str],
) -> dict[tuple[str, str, int], dict[int, list[dict[str, Any]]]]:
    attempts: dict[tuple[str, str, int], dict[int, list[dict[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in state.rows:
        identity = results.router_identity_from_row(row)
        if identity.experiment_digest != experiment.digest:
            raise RouterRunError(
                "results file contains a different Router Bench experiment"
            )
        coordinate = (identity.task, identity.window_id, identity.repetition)
        attempts[coordinate][identity.block_attempt].append(row)
    for coordinate, by_attempt in attempts.items():
        numbers = sorted(by_attempt)
        if numbers != list(range(numbers[-1] + 1)):
            raise RouterRunError(
                f"results have non-contiguous block attempts for {coordinate}"
            )
        for attempt, rows_for_attempt in by_attempt.items():
            arm_ids = {
                results.router_identity_from_row(row).arm_id
                for row in rows_for_attempt
            }
            if not arm_ids <= expected_arms:
                raise RouterRunError(
                    f"results contain unknown arms for {coordinate} attempt {attempt}"
                )
    return attempts


def _validate_resume_identities(
    state: results.ResumeState,
    *,
    experiment: router_spec.RouterExperiment,
    schedule: Sequence[ScheduleBlock],
    tasks: Mapping[str, TaskProvenance],
    arms: Mapping[str, router_spec.Arm],
    digests: Mapping[str, str],
    harness_version: str,
    exec_mode: str,
    image_digest: str | None,
) -> None:
    blocks = {block.coordinate: block for block in schedule}
    for row in state.rows:
        actual = results.router_identity_from_row(row)
        coordinate = (actual.task, actual.window_id, actual.repetition)
        try:
            expected = _identity(
                experiment=experiment,
                arm=arms[actual.arm_id],
                task=tasks[actual.task],
                block=blocks[coordinate],
                attempt=actual.block_attempt,
                digests=digests,
                harness_version=harness_version,
                exec_mode=exec_mode,
                image_digest=image_digest,
            )
        except KeyError as exc:
            raise RouterRunError(
                f"results contain an unscheduled cell: {coordinate} arm={actual.arm_id}"
            ) from exc
        if actual != expected:
            raise RouterRunError(
                f"results identity does not match the current run for "
                f"{coordinate} arm={actual.arm_id} attempt={actual.block_attempt}"
            )


def _attempt_complete_and_valid(
    rows_for_attempt: Sequence[Mapping[str, Any]], expected_arms: set[str]
) -> bool:
    if {
        results.router_identity_from_row(row).arm_id for row in rows_for_attempt
    } != expected_arms:
        return False
    return all(
        row.get("result", {}).get("infrastructure_invalid_reason") is None
        and row.get("route_integrity", {}).get("pass") is True
        for row in rows_for_attempt
    )


def _next_attempt(
    by_attempt: Mapping[int, Sequence[Mapping[str, Any]]],
    expected_arms: set[str],
    force: bool,
) -> tuple[int | None, bool]:
    if not by_attempt:
        return 0, False
    latest = max(by_attempt)
    valid = _attempt_complete_and_valid(by_attempt[latest], expected_arms)
    if valid and not force:
        return None, True
    return latest + 1, False


def _sanitized_env(
    *,
    adapters_dir: Path,
    instruction_path: Path,
    workdir: Path,
    route_plan_path: Path,
    proxy_base_url: str,
    token: str,
) -> dict[str, str]:
    package_root = str(Path(__file__).resolve().parent.parent)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": package_root,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "BENCH_ADAPTERS_DIR": str(adapters_dir),
        "BENCH_INSTRUCTION_PATH": str(instruction_path),
        "BENCH_WORKDIR": str(workdir),
        "OPENBENCH_ROUTE_PLAN_PATH": str(route_plan_path),
        "OPENBENCH_PROXY": "1",
        "OPENBENCH_PROXY_BASE_URL": proxy_base_url,
        "OPENBENCH_PROXY_CELL_TOKEN": token,
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    return {key: value for key, value in env.items() if value}


def _parse_entry_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith(_RESULT_SENTINEL):
            try:
                value = json.loads(line[len(_RESULT_SENTINEL):].strip())
            except json.JSONDecodeError as exc:
                raise RouterRunError("routed entry emitted invalid result JSON") from exc
            if not isinstance(value, dict):
                raise RouterRunError("routed entry result must be an object")
            return value
    raise RouterRunError("routed entry emitted no result sentinel")


def _invoke_local(
    *,
    plan_path: Path,
    instruction_path: Path,
    workdir: Path,
    proxy_base_url: str,
    token: str,
    timeout_s: int,
    adapters_dir: Path,
) -> dict[str, Any]:
    env = _sanitized_env(
        adapters_dir=adapters_dir,
        instruction_path=instruction_path,
        workdir=workdir,
        route_plan_path=plan_path,
        proxy_base_url=proxy_base_url,
        token=token,
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "obench.entry",
            "pi",
            "routed",
            str(timeout_s),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s + 10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        return {
            "completed": False,
            "error": f"entry timeout after {timeout_s}s",
            "output_tail": (stdout + stderr)[-2000:],
            "entry_timed_out": True,
        }
    if proc.returncode != 0:
        raise RouterRunError(
            f"routed entry exited {proc.returncode}: {(stderr or stdout)[-1000:]}"
        )
    return _parse_entry_result(stdout)


def _read_sealed_ledger(seal: proxy.LedgerSeal, arm_digest: str) -> list[dict[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in seal.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise RouterRunError(f"cannot read sealed proxy ledger: {exc}") from exc
    if not rows or rows[-1].get("record_type") != "ledger_seal":
        raise RouterRunError("proxy ledger has no terminal seal")
    terminal = rows[-1]
    if (
        terminal.get("record_count") != seal.record_count
        or terminal.get("last_sequence") != seal.last_sequence
        or terminal.get("root_hash") != seal.root_hash
        or len(rows) != seal.record_count + 1
    ):
        raise RouterRunError("proxy ledger terminal seal does not match")
    previous = _EMPTY_HASH
    for sequence, row in enumerate(rows[:-1], 1):
        record = dict(row)
        record_hash = record.pop("record_hash", None)
        if (
            record.get("record_type") != "request"
            or record.get("sequence") != sequence
            or record.get("previous_hash") != previous
            or row.get("router_arm", {}).get("arm_digest") != arm_digest
        ):
            raise RouterRunError("proxy ledger chain or arm binding is invalid")
        expected = _digest_bytes(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if record_hash != expected:
            raise RouterRunError("proxy ledger record hash is invalid")
        previous = record_hash
    if previous != seal.root_hash:
        raise RouterRunError("proxy ledger root hash is invalid")
    return rows[:-1]


def _price_call(
    metrics: Mapping[str, Any],
    prices: Mapping[str, Price],
    plan: router_spec.RoutePlan,
) -> tuple[dict[str, Any], Decimal | None]:
    route = metrics.get("route") if isinstance(metrics.get("route"), dict) else {}
    usage = metrics.get("usage") if isinstance(metrics.get("usage"), dict) else {}
    price = prices.get(plan.canonical_model)
    if price is None:
        return {}, None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
    ):
        return {}, None
    amount = (
        Decimal(input_tokens) * price.input_per_million
        + Decimal(output_tokens) * price.output_per_million
    ) / Decimal(1_000_000)
    evidence = {
        "frozen_list_estimate": {
            "amount_usd": float(amount),
            "currency": "USD",
            "effective_at": price.effective_at,
        }
    }
    return evidence, amount


def _route_reasons(metrics: Mapping[str, Any], plan: router_spec.RoutePlan) -> list[str]:
    route = metrics.get("route")
    stream = metrics.get("stream")
    if not isinstance(route, Mapping):
        return ["missing_route_evidence"]
    reasons = []
    evidence = metrics.get("route_evidence")
    if not isinstance(evidence, Mapping):
        reasons.append("missing_route_evidence_verdict")
    elif evidence.get("pass") is not True:
        evidence_reasons = evidence.get("reasons")
        if isinstance(evidence_reasons, list) and all(
            isinstance(reason, str) and reason for reason in evidence_reasons
        ):
            reasons.extend(evidence_reasons)
        else:
            reasons.append("route_evidence_failed")
    if route.get("requested_model") != plan.requested_model:
        reasons.append("requested_model_conflict")
    flexible_gateway_model = plan.gateway == "vercel"
    served_model = route.get("served_model")
    served_model_matches = (
        isinstance(served_model, str)
        and (
            served_model in plan.allowed_models
            if flexible_gateway_model
            else served_model == plan.requested_model
        )
    )
    if not served_model_matches:
        reasons.append("served_model_conflict")
    provider = route.get("provider")
    if (
        plan.route_kind == "gateway"
        and (
            not isinstance(provider, str)
            or provider.casefold() != plan.requested_provider.casefold()
        )
    ):
        reasons.append("provider_conflict")
    if not isinstance(stream, Mapping) or stream.get("done") is not True:
        reasons.append("stream_not_done")
    attempts = route.get("attempts")
    if plan.route_kind == "gateway":
        if route.get("metadata_requested_model") != plan.requested_model:
            reasons.append("metadata_requested_model_conflict")
        if attempts is not None:
            if not isinstance(attempts, list):
                reasons.append("malformed_attempts")
                return sorted(set(reasons))
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    reasons.append("malformed_attempt")
                    continue
                if str(attempt.get("provider", "")).casefold() != plan.requested_provider.casefold():
                    reasons.append("attempt_provider_conflict")
                attempt_model = attempt.get("model")
                attempt_model_matches = (
                    isinstance(attempt_model, str)
                    and (
                        attempt_model in plan.allowed_models
                        if flexible_gateway_model
                        else attempt_model == plan.requested_model
                    )
                )
                if not attempt_model_matches:
                    reasons.append("attempt_model_conflict")
    return sorted(set(reasons))


def _proxy_evidence(
    ledger_rows: Sequence[Mapping[str, Any]],
    prices: Mapping[str, Price],
    budget: router_spec.Budget,
    plan: router_spec.RoutePlan,
) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    calls = []
    reasons = []
    total_output = 0
    total_cost = Decimal(0)
    priced_calls = 0
    successful_calls = 0
    auth_failure = False
    for row in ledger_rows:
        status = row.get("status")
        if isinstance(status, int) and 400 <= status <= 599:
            if status in {401, 403, 407}:
                auth_failure = True
            calls.append({
                "timing": None,
                "generation": None,
                "route": {
                    "provider": plan.requested_provider,
                    "served_model": plan.requested_model,
                },
                "costs": None,
            })
            continue
        metrics = row.get("router_metrics")
        if not isinstance(metrics, dict):
            reasons.append("missing_router_metrics")
            calls.append({
                "timing": None,
                "generation": None,
                "route": {
                    "provider": plan.requested_provider,
                    "served_model": plan.requested_model,
                },
                "costs": None,
            })
            continue
        successful_calls += 1
        reasons.extend(_route_reasons(metrics, plan))
        usage = metrics.get("usage") if isinstance(metrics.get("usage"), dict) else {}
        output_tokens = usage.get("output_tokens")
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
            total_output += output_tokens
        costs, amount = _price_call(metrics, prices, plan)
        if amount is not None:
            total_cost += amount
            priced_calls += 1
        normalized_route = metrics.get("route")
        if isinstance(normalized_route, dict):
            normalized_route = dict(normalized_route)
            if plan.route_kind == "direct" and not normalized_route.get("provider"):
                normalized_route["provider"] = plan.requested_provider
        calls.append(
            {
                "timing": metrics.get("timing"),
                "generation": metrics.get("generation"),
                "route": normalized_route,
                "costs": costs,
            }
        )
    infrastructure_reason = None
    if auth_failure:
        infrastructure_reason = "upstream_auth_failure"
    elif len(ledger_rows) > budget.max_calls:
        infrastructure_reason = "max_calls_exceeded"
    elif total_output > budget.max_output_tokens:
        infrastructure_reason = "max_output_tokens_exceeded"
    elif successful_calls and priced_calls != successful_calls:
        infrastructure_reason = "usd_cap_unenforceable_no_frozen_price"
    elif total_cost > Decimal(budget.usd_cap):
        infrastructure_reason = "usd_cap_exceeded"
    route_integrity = {
        "pass": not reasons,
        "reasons": sorted(set(reasons)),
    }
    return calls, route_integrity, infrastructure_reason


def _append_row(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        row, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
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


def _run_cell(
    *,
    experiment: router_spec.RouterExperiment,
    arm: router_spec.Arm,
    plan: router_spec.RoutePlan,
    secret_plan: router_spec.SecretPlan,
    task: TaskProvenance,
    block: ScheduleBlock,
    attempt: int,
    identity: results.CellIdentity,
    server: proxy.CountingProxyServer,
    proxy_base_url: str,
    prices: Mapping[str, Price],
    exec_mode: str,
    adapters_dir: Path,
) -> dict[str, Any]:
    token = "cell-" + hashlib.sha256(
        f"{results.make_router_cell_id(identity)}:{time.time_ns()}".encode()
    ).hexdigest()[:32]
    server.register_cell(token)
    server.register_route(token, plan, secret_plan)
    started = time.monotonic()
    infrastructure_reason = None
    adapter_result: dict[str, Any] = {}
    seal = None
    ledger_rows: list[dict[str, Any]] = []
    route_integrity = {"pass": False, "reasons": ["ledger_not_sealed"]}
    calls: list[dict[str, Any]] = []
    checker_exit: int | str | None = None
    checker_score = 0.0
    checker_stdout = ""
    checker_stderr = ""
    with tempfile.TemporaryDirectory(prefix="obench_router_cell_") as temp:
        cell_root = Path(temp)
        workdir = cell_root / "workspace"
        workdir.mkdir()
        plan_path = cell_root / "route-plan.json"
        instruction_path = cell_root / "instruction.txt"
        plan_path.write_text(plan.canonical_json + "\n", encoding="utf-8")
        instruction = read_instruction(str(task.path))
        instruction_path.write_text(instruction, encoding="utf-8")
        try:
            materialize_workspace(str(task.path), str(workdir))
            if _tree_digest(workdir) != task.workspace_source_sha:
                raise RouterRunError("materialized workspace differs from frozen provenance")
        except (WorkspaceError, RouterRunError) as exc:
            infrastructure_reason = "workspace_materialization"
            adapter_result = {"completed": False, "error": str(exc)}
        else:
            try:
                adapter_result = _invoke_local(
                    plan_path=plan_path,
                    instruction_path=instruction_path,
                    workdir=workdir,
                    proxy_base_url=proxy_base_url,
                    token=token,
                    timeout_s=experiment.budget.timeout_s,
                    adapters_dir=adapters_dir,
                )
            except Exception as exc:  # noqa: BLE001 - row records infra failure
                infrastructure_reason = "adapter_entry_failure"
                adapter_result = {"completed": False, "error": str(exc)}
        try:
            seal = server.seal_cell(token, timeout_s=10)
            ledger_rows = _read_sealed_ledger(seal, arm.digest)
            calls, route_integrity, budget_reason = _proxy_evidence(
                ledger_rows, prices, experiment.budget, plan
            )
            infrastructure_reason = infrastructure_reason or budget_reason
        except Exception as exc:  # noqa: BLE001 - sealing is evidence-critical
            infrastructure_reason = infrastructure_reason or "ledger_seal_failure"
            adapter_result.setdefault("error", str(exc))

        if not ledger_rows and infrastructure_reason is None:
            infrastructure_reason = "adapter_no_routed_calls"
        if (
            adapter_result.get("entry_timed_out")
            and not ledger_rows
            and infrastructure_reason is None
        ):
            infrastructure_reason = "adapter_entry_timeout"

        # Checker authority begins only after the proxy has reached terminal state.
        if seal is not None:
            try:
                checker_home = cell_root / "checker-home"
                checker_home.mkdir()
                checker_env = {
                    key: os.environ[key]
                    for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
                    if os.environ.get(key)
                }
                checker_env["HOME"] = str(checker_home)
                checker_exit, raw_score, checker_stdout, checker_stderr = run_checker(
                    str(task.path), str(workdir), CHECKER_TIMEOUT_S, checker_env
                )
                checker_score = (
                    1.0
                    if checker_exit == 0
                    else raw_score if raw_score is not None else 0.0
                )
                if checker_exit == "timeout":
                    infrastructure_reason = infrastructure_reason or "checker_timeout"
            except Exception as exc:  # noqa: BLE001
                infrastructure_reason = infrastructure_reason or "checker_failure"
                checker_stderr = str(exc)
    duration = time.monotonic() - started
    solved = checker_exit == 0
    available = bool(ledger_rows) and all(
        isinstance(row.get("status"), int) and 200 <= row["status"] < 300
        for row in ledger_rows
    )
    timed_out = bool(adapter_result.get("entry_timed_out")) or "timeout" in str(
        adapter_result.get("error", "")
    ).lower()
    if not adapter_result.get("completed", False) and not ledger_rows:
        infrastructure_reason = infrastructure_reason or "adapter_failed_before_route"
    failure_class = (
        "infrastructure"
        if infrastructure_reason is not None
        else "treatment"
        if not solved or not available or not adapter_result.get("completed", False)
        else None
    )
    expected_arm_ids = [item.arm_id for item in experiment.arms]
    row = {
        "schema_version": results.CURRENT_SCHEMA_VERSION,
        "benchmark": results.ROUTER_BENCHMARK,
        "run_id": results.make_router_run_id(identity),
        "cell_id": results.make_router_cell_id(identity),
        "identity": identity.as_dict(),
        "expected_arm_ids": expected_arm_ids,
        "arm_role": arm.route_kind,
        "baseline": arm.baseline,
        "result": {
            "solved": solved,
            "checker_score": checker_score,
            "available": available,
            "duration_s": duration,
            "timed_out": timed_out,
            "infrastructure_invalid_reason": infrastructure_reason,
            "failure_class": failure_class,
            "adapter_completed": bool(adapter_result.get("completed", False)),
            "adapter_error": adapter_result.get("error"),
            "checker_exit": checker_exit,
        },
        "route_integrity": route_integrity,
        "proxy_metrics": {"calls": calls},
        "ledger_seal": (
            {
                "record_count": seal.record_count,
                "last_sequence": seal.last_sequence,
                "root_hash": seal.root_hash,
                "ledger_file": seal.path.name,
            }
            if seal is not None
            else None
        ),
        "checker": {
            "stdout_tail": checker_stdout[-4000:],
            "stderr_tail": checker_stderr[-4000:],
        },
        "route_isolation": {
            "classification": "exploratory",
            "lane": _lane(exec_mode),
            "egress_enforced": False,
        },
        "schedule_order": list(block.arm_ids),
        "block_attempt": attempt,
    }
    results.result_cell_id(row)
    return row


def validate_experiment(
    experiment_path: str | os.PathLike[str],
    *,
    tasks_dir: str | os.PathLike[str],
) -> tuple[router_spec.RouterExperiment, dict[str, TaskProvenance]]:
    experiment = router_spec.load_experiment(experiment_path)
    tasks = inspect_tasks(experiment, tasks_dir)
    build_schedule(experiment)
    return experiment, tasks


def doctor_experiment(
    experiment_path: str | os.PathLike[str],
    *,
    tasks_dir: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    experiment, tasks = validate_experiment(experiment_path, tasks_dir=tasks_dir)
    admitted = {arm.auth_env for arm in experiment.arms}
    router_spec.compile_route_plans(
        experiment, environ=environ, admitted_auth_envs=admitted
    )
    prices, price_snapshot = _load_prices(environ)
    missing_prices = _missing_price_models(experiment, prices)
    version = pi.version()
    if not version:
        raise RouterRunError("Pi CLI is unavailable or did not report a version")
    return {
        "experiment_id": experiment.experiment_id,
        "experiment_digest": experiment.digest,
        "tasks": len(tasks),
        "arms": len(experiment.arms),
        "pi_version": version,
        "prices": "frozen" if prices and not missing_prices else "incomplete",
        "missing_price_models": missing_prices,
        "usd_cap_enforceable": bool(prices) and not missing_prices,
        "route_isolation": "exploratory",
    }


def run_experiment(
    experiment_path: str | os.PathLike[str],
    *,
    results_path: str | os.PathLike[str],
    tasks_dir: str | os.PathLike[str],
    exec_mode: str | None = None,
    force: bool = False,
    environ: Mapping[str, str] | None = None,
    adapters_dir: str | os.PathLike[str] | None = None,
) -> RunSummary:
    """Run or safely resume one Gateway Tax experiment."""
    environ = os.environ if environ is None else environ
    experiment, tasks = validate_experiment(experiment_path, tasks_dir=tasks_dir)
    exec_mode = exec_mode or experiment.execution_lane
    if exec_mode not in {"local", "docker"}:
        raise RouterRunError("exec_mode must be 'local' or 'docker'")
    if exec_mode == "docker":
        raise RouterRunError(
            "Docker Gateway Tax execution is exploratory and unsupported in this MVP"
        )
    admitted = {arm.auth_env for arm in experiment.arms}
    plans, secret_plan = router_spec.compile_route_plans(
        experiment, environ=environ, admitted_auth_envs=admitted
    )
    prices, price_snapshot = _load_prices(environ)
    if not prices:
        raise RouterRunError(
            f"USD cap cannot be enforced without {FROZEN_PRICES_ENV}; refusing to run"
        )
    missing_prices = _missing_price_models(experiment, prices)
    if missing_prices:
        raise RouterRunError(
            "USD cap cannot be enforced; frozen prices are missing: "
            + ", ".join(missing_prices)
        )
    full_schedule = build_schedule(experiment)
    schedule = _active_schedule(experiment, full_schedule)
    if not schedule:
        raise RouterRunError("no experiment window is currently active")
    digests = _comparison_digests(experiment, full_schedule, price_snapshot)
    path = Path(results_path).resolve()
    persist_price_snapshot(path, price_snapshot)
    state = results.read_jsonl_for_resume(path)
    expected_arms = {arm.arm_id for arm in experiment.arms}
    by_arm = {arm.arm_id: arm for arm in experiment.arms}
    plans_by_arm = {plan.arm_id: plan for plan in plans}
    version = pi.version() or "unknown"
    image = None
    _validate_resume_identities(
        state,
        experiment=experiment,
        schedule=full_schedule,
        tasks=tasks,
        arms=by_arm,
        digests=digests,
        harness_version=version,
        exec_mode=exec_mode,
        image_digest=image,
    )
    attempts = _existing_attempts(state, experiment, expected_arms)
    adapters = (
        Path(adapters_dir).resolve()
        if adapters_dir is not None
        else Path(environ.get(ADAPTERS_DIR_ENV, Path(__file__).resolve().parent / "adapters"))
        .resolve()
    )
    if not (adapters / "pi.py").is_file():
        raise RouterRunError(f"Pi adapter not found in {adapters}")

    ledger_dir = path.parent / f".{path.stem}.router-ledgers"
    server, thread = proxy.start_in_thread(
        "127.0.0.1",
        0,
        ledger_dir,
        require_registered_tokens=False,
        timeout_s=experiment.budget.timeout_s,
    )
    port = server.server_address[1]
    local_base = f"http://127.0.0.1:{port}"
    rows_appended = 0
    blocks_completed = 0
    blocks_replaced = 0
    blocks_skipped = 0
    try:
        for block in schedule:
            by_attempt = attempts.get(block.coordinate, {})
            attempt, skipped = _next_attempt(by_attempt, expected_arms, force)
            if skipped:
                blocks_skipped += 1
                continue
            assert attempt is not None
            if attempt > 0:
                blocks_replaced += 1
            block_rows = []
            for arm_id in block.arm_ids:
                arm = by_arm[arm_id]
                identity = _identity(
                    experiment=experiment,
                    arm=arm,
                    task=tasks[block.task],
                    block=block,
                    attempt=attempt,
                    digests=digests,
                    harness_version=version,
                    exec_mode=exec_mode,
                    image_digest=image,
                )
                row = _run_cell(
                    experiment=experiment,
                    arm=arm,
                    plan=plans_by_arm[arm_id],
                    secret_plan=secret_plan,
                    task=tasks[block.task],
                    block=block,
                    attempt=attempt,
                    identity=identity,
                    server=server,
                    proxy_base_url=local_base,
                    prices=prices,
                    exec_mode=exec_mode,
                    adapters_dir=adapters,
                )
                _append_row(path, row)
                rows_appended += 1
                block_rows.append(row)
            if _attempt_complete_and_valid(block_rows, expected_arms):
                blocks_completed += 1
            else:
                raise RouterRunError(
                    f"block {block.coordinate} attempt {attempt} is invalid; "
                    "paid replacement requires an explicit rerun"
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return RunSummary(
        results_path=path,
        rows_appended=rows_appended,
        blocks_completed=blocks_completed,
        blocks_replaced=blocks_replaced,
        blocks_skipped=blocks_skipped,
    )
