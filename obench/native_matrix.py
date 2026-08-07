"""Deterministic matched planning for native Computer-Use comparisons."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence


PLAN_SCHEMA_VERSION = "openbench.native-matrix.v1"
STATE_SCHEMA_VERSION = "openbench.native-matrix-state.v1"
PILOT_REPETITIONS = 5
PUBLISH_RECOMMENDED_REPETITIONS = 10
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?\Z")


class NativeMatrixError(ValueError):
    """Raised when a native comparison plan or resume state is ambiguous."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _identity(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise NativeMatrixError(f"{location} must be a non-empty object")
    normalized = dict(value)
    try:
        canonical_bytes(normalized)
    except (TypeError, ValueError) as exc:
        raise NativeMatrixError(f"{location} must be canonical JSON: {exc}") from exc
    return normalized


def _identifier(value: Any, location: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise NativeMatrixError(
            f"{location} must match lowercase [a-z0-9._-] identifier syntax"
        )
    return value


def _positive_integer(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise NativeMatrixError(f"{location} must be a positive integer")
    return value


def _balanced_order(arm_ids: Sequence[str], repetition: int) -> list[str]:
    """Return forward/reverse rotated orders; for two arms this is AB/BA."""
    rotation = (repetition // 2) % len(arm_ids)
    rotated = list(arm_ids[rotation:]) + list(arm_ids[:rotation])
    return rotated if repetition % 2 == 0 else list(reversed(rotated))


def build_native_matrix(
    *,
    comparison_id: str,
    task: Mapping[str, Any],
    harness: Mapping[str, Any],
    model: Mapping[str, Any],
    arms: Sequence[Mapping[str, Any]],
    repetitions: int = PILOT_REPETITIONS,
) -> dict[str, Any]:
    """Build one immutable, matched and interleaved native comparison plan.

    Each arm must contain ``id`` and ``mcp``. An optional ``config`` object may
    bind additional arm-specific settings. Task, harness, and model identities
    are shared by every cell and included in every config digest.
    """
    comparison_id = _identifier(comparison_id, "comparison_id")
    repetitions = _positive_integer(repetitions, "repetitions")
    fixed = {
        "task": _identity(task, "task"),
        "harness": _identity(harness, "harness"),
        "model": _identity(model, "model"),
    }
    if not isinstance(arms, Sequence) or isinstance(arms, (str, bytes)) or len(arms) < 2:
        raise NativeMatrixError("arms must contain at least two MCP arm objects")

    normalized_arms: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_configs: set[str] = set()
    for index, raw in enumerate(arms):
        if not isinstance(raw, Mapping):
            raise NativeMatrixError(f"arms[{index}] must be an object")
        arm_id = _identifier(raw.get("id"), f"arms[{index}].id")
        if arm_id in seen_ids:
            raise NativeMatrixError(f"duplicate arm id {arm_id!r}")
        unexpected = set(raw) - {"id", "mcp", "config"}
        if unexpected:
            raise NativeMatrixError(
                f"arms[{index}] has unexpected fields: {sorted(unexpected)!r}"
            )
        config_identity = {
            **fixed,
            "mcp": _identity(raw.get("mcp"), f"arms[{index}].mcp"),
            "arm_config": _identity(raw.get("config", {"profile": "default"}), f"arms[{index}].config"),
        }
        config_sha256 = canonical_sha256(config_identity)
        if config_sha256 in seen_configs:
            raise NativeMatrixError("MCP arms must have distinct exact configurations")
        normalized_arms.append(
            {
                "id": arm_id,
                "config_sha256": config_sha256,
                "mcp_identity_sha256": canonical_sha256(config_identity["mcp"]),
                "config_identity": config_identity,
            }
        )
        seen_ids.add(arm_id)
        seen_configs.add(config_sha256)

    arm_ids = [arm["id"] for arm in normalized_arms]
    by_id = {arm["id"]: arm for arm in normalized_arms}
    schedule: list[dict[str, Any]] = []
    sequence = 0
    for repetition_index in range(repetitions):
        block = repetition_index + 1
        order = _balanced_order(arm_ids, repetition_index)
        for position, arm_id in enumerate(order, 1):
            sequence += 1
            arm = by_id[arm_id]
            trial_id = f"{comparison_id}-{arm_id}-trial{block}"
            cell_identity = {
                "comparison_id": comparison_id,
                "block": block,
                "arm_id": arm_id,
                "trial_id": trial_id,
                "config_sha256": arm["config_sha256"],
            }
            schedule.append(
                {
                    "sequence": sequence,
                    "block": block,
                    "position": position,
                    "arm_id": arm_id,
                    "cell_id": f"block{block}:{arm_id}",
                    "trial_id": trial_id,
                    "config_sha256": arm["config_sha256"],
                    "cell_sha256": canonical_sha256(cell_identity),
                }
            )

    body = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "comparison_id": comparison_id,
        "methodology": {
            "design": "matched_interleaved_forward_reverse",
            "binary_interval": "wilson_95",
            "continuous_summary": "median_p95",
            "score_judge": "deterministic_verifier_only",
            "execution_backend": "native_macos",
        },
        "fixed_identity": fixed,
        "fixed_identity_sha256": canonical_sha256(fixed),
        "repetitions": repetitions,
        "pilot_default_repetitions": PILOT_REPETITIONS,
        "publish_recommended_repetitions": PUBLISH_RECOMMENDED_REPETITIONS,
        "publish_repetition_recommendation_met": (
            repetitions >= PUBLISH_RECOMMENDED_REPETITIONS
        ),
        "arms": normalized_arms,
        "schedule": schedule,
    }
    return {**body, "plan_sha256": canonical_sha256(body)}


def validate_native_matrix(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild a plan from its declared intent and require exact canonical form."""
    if not isinstance(plan, Mapping):
        raise NativeMatrixError("plan must be an object")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise NativeMatrixError(f"plan schema_version must be {PLAN_SCHEMA_VERSION!r}")
    fixed = plan.get("fixed_identity")
    if not isinstance(fixed, Mapping):
        raise NativeMatrixError("plan.fixed_identity must be an object")
    raw_arms = plan.get("arms")
    if not isinstance(raw_arms, list):
        raise NativeMatrixError("plan.arms must be an array")
    rebuilt = build_native_matrix(
        comparison_id=plan.get("comparison_id"),
        task=fixed.get("task"),
        harness=fixed.get("harness"),
        model=fixed.get("model"),
        arms=[
            {
                "id": arm.get("id"),
                "mcp": arm.get("config_identity", {}).get("mcp"),
                "config": arm.get("config_identity", {}).get("arm_config"),
            }
            for arm in raw_arms
            if isinstance(arm, Mapping)
        ],
        repetitions=plan.get("repetitions"),
    )
    if dict(plan) != rebuilt:
        raise NativeMatrixError("plan does not match its canonical declared intent")
    return rebuilt


def reconcile_native_state(
    plan: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]] = (),
    *,
    prior_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create resumable state without replacing or reassigning completed cells."""
    validated = validate_native_matrix(plan)
    expected = {cell["cell_id"]: cell for cell in validated["schedule"]}
    completed: dict[str, dict[str, Any]] = {}

    if prior_state is not None:
        if (
            prior_state.get("schema_version") != STATE_SCHEMA_VERSION
            or prior_state.get("plan_sha256") != validated["plan_sha256"]
        ):
            raise NativeMatrixError("resume state is not bound to this exact plan")
        prior_completed = prior_state.get("completed")
        if not isinstance(prior_completed, list):
            raise NativeMatrixError("resume state completed must be an array")
        observations = [*prior_completed, *observations]

    required = {
        "cell_id",
        "trial_id",
        "config_sha256",
        "cell_sha256",
        "result_sha256",
        "bundle_sha256",
    }
    for index, raw in enumerate(observations):
        if not isinstance(raw, Mapping):
            raise NativeMatrixError(f"observations[{index}] must be an object")
        if set(raw) != required:
            raise NativeMatrixError(
                f"observations[{index}] must contain exactly {sorted(required)!r}"
            )
        cell_id = raw.get("cell_id")
        cell = expected.get(cell_id)
        if cell is None:
            raise NativeMatrixError(f"observation references unknown cell {cell_id!r}")
        for field in ("trial_id", "config_sha256", "cell_sha256"):
            if raw.get(field) != cell[field]:
                raise NativeMatrixError(
                    f"observation {cell_id!r} has conflicting {field}"
                )
        for field in ("result_sha256", "bundle_sha256"):
            if not isinstance(raw.get(field), str) or _DIGEST_RE.fullmatch(raw[field]) is None:
                raise NativeMatrixError(f"observation {cell_id!r} has invalid {field}")
        normalized = dict(raw)
        previous = completed.get(cell_id)
        if previous is not None and previous != normalized:
            raise NativeMatrixError(
                f"cell {cell_id!r} already has different immutable result evidence"
            )
        completed[cell_id] = normalized

    ordered = [
        completed[cell["cell_id"]]
        for cell in validated["schedule"]
        if cell["cell_id"] in completed
    ]
    pending = [
        cell["cell_id"]
        for cell in validated["schedule"]
        if cell["cell_id"] not in completed
    ]
    body = {
        "schema_version": STATE_SCHEMA_VERSION,
        "plan_sha256": validated["plan_sha256"],
        "completed": ordered,
        "pending_cell_ids": pending,
    }
    return {**body, "state_sha256": canonical_sha256(body)}


__all__ = [
    "NativeMatrixError",
    "PILOT_REPETITIONS",
    "PLAN_SCHEMA_VERSION",
    "PUBLISH_RECOMMENDED_REPETITIONS",
    "STATE_SCHEMA_VERSION",
    "build_native_matrix",
    "canonical_bytes",
    "canonical_sha256",
    "reconcile_native_state",
    "validate_native_matrix",
]
