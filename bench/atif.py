"""ATIF schema helpers for derived OpenBench trajectory exports.

Raw OpenBench transcripts remain the source of truth. ATIF JSON is derived from
those local, unscrubbed transcripts; scrub and review trajectories before
sharing them outside the local machine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any
import json
import math

SCHEMA_VERSION = "ATIF-v1.7"
SOURCES = {"system", "user", "agent"}
TOOL_CALL_FIELDS = {"tool_call_id", "function_name", "arguments", "extra"}
OBSERVATION_FIELDS = {"results"}
OBSERVATION_RESULT_FIELDS = {"source_call_id", "content", "subagent_trajectory_ref", "extra"}


@dataclass
class Metrics:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Step:
    step_id: int
    source: str
    message: str | list[dict[str, Any]]
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    observation: dict[str, Any] | None = None
    metrics: Metrics | dict[str, Any] | None = None
    timestamp: str | None = None
    model_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trajectory:
    agent: dict[str, Any]
    steps: list[Step | dict[str, Any]]
    schema_version: str = SCHEMA_VERSION
    final_metrics: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    trajectory_id: str | None = None
    notes: str | None = None


def _clean(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None and v != {} and v != []}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def to_dict(trajectory: Trajectory | dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable trajectory dict with empty optional fields pruned."""
    data = _clean(trajectory)
    if not isinstance(data, dict):
        raise TypeError("trajectory must be a Trajectory or dict")
    return data


def dump_trajectory(trajectory: Trajectory | dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(to_dict(trajectory), indent=2, ensure_ascii=False) + "\n")


class ATIFValidationError(ValueError):
    """Raised when an ATIF trajectory fails validation."""


def _is_non_negative_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 and math.isfinite(float(value))


def _is_non_negative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_content_parts(value: Any, location: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        return
    for index, part in enumerate(value):
        loc = f"{location}[{index}]"
        if not isinstance(part, dict):
            errors.append(f"{loc}: content part must be an object")
            continue
        part_type = part.get("type")
        if part_type == "text":
            if not isinstance(part.get("text"), str):
                errors.append(f"{loc}.text: required string when type is 'text'")
            if "source" in part:
                errors.append(f"{loc}.source: not allowed when type is 'text'")
        elif part_type == "image":
            source = part.get("source")
            if not isinstance(source, dict):
                errors.append(f"{loc}.source: required object when type is 'image'")
            else:
                if source.get("media_type") not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                    errors.append(f"{loc}.source.media_type: unsupported image media type")
                if not isinstance(source.get("path"), str) or not source.get("path"):
                    errors.append(f"{loc}.source.path: required non-empty string")
            if "text" in part:
                errors.append(f"{loc}.text: not allowed when type is 'image'")
        else:
            errors.append(f"{loc}.type: expected 'text' or 'image', got {part_type!r}")


def validate_trajectory(trajectory: dict[str, Any] | str | Path) -> list[str]:
    """Validate the OpenBench ATIF subset.

    Returns a list of all validation errors. An empty list means valid. The
    checks intentionally mirror the RFC/Harbor validator surfaces OpenBench
    relies on without importing Pydantic: required fields, sequential step ids,
    allowed sources, tool/observation references, and aggregate metric
    consistency.
    """
    data: Any = trajectory
    if isinstance(trajectory, Path):
        try:
            data = json.loads(trajectory.read_text())
        except Exception as exc:  # noqa: BLE001 - collect as validation error
            return [f"Invalid JSON: {exc}"]
    elif isinstance(trajectory, str):
        try:
            data = json.loads(trajectory)
        except json.JSONDecodeError as json_exc:
            try:
                candidate = Path(trajectory)
                if candidate.exists():
                    data = json.loads(candidate.read_text())
                else:
                    return [f"Input string is not valid JSON or a file path: {json_exc}"]
            except OSError:
                return [f"Input string is not valid JSON or a file path: {json_exc}"]
            except Exception as file_exc:  # noqa: BLE001 - collect as validation error
                return [f"Invalid JSON: {file_exc}"]

    errors: list[str] = []
    if not isinstance(data, dict):
        return ["trajectory must be an object"]

    for field_name in ("schema_version", "agent", "steps"):
        if field_name not in data:
            errors.append(f"trajectory.{field_name}: required field is missing")
    if errors:
        return errors

    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"trajectory.schema_version: expected {SCHEMA_VERSION!r}")

    agent = data.get("agent")
    if not isinstance(agent, dict):
        errors.append("trajectory.agent: must be an object")
    else:
        for field_name in ("name", "version"):
            if not isinstance(agent.get(field_name), str) or not agent.get(field_name):
                errors.append(f"trajectory.agent.{field_name}: required non-empty string")

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("trajectory.steps: required non-empty array")
        steps = []

    metric_sums = {
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cached_tokens": 0,
        "total_cost_usd": 0.0,
    }
    saw_cost = False

    for index, step in enumerate(steps):
        loc = f"trajectory.steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{loc}: must be an object")
            continue
        expected_id = index + 1
        if step.get("step_id") != expected_id:
            errors.append(f"{loc}.step_id: expected {expected_id}, got {step.get('step_id')!r}")
        source = step.get("source")
        if source not in SOURCES:
            errors.append(f"{loc}.source: expected one of {sorted(SOURCES)}, got {source!r}")
        if "message" not in step:
            errors.append(f"{loc}.message: required field is missing")
        elif not isinstance(step.get("message"), (str, list)):
            errors.append(f"{loc}.message: must be string or content array")
        else:
            _validate_content_parts(step.get("message"), f"{loc}.message", errors)
        if source != "agent":
            for name in ("reasoning_content", "tool_calls", "metrics", "model_name"):
                if name in step and step.get(name) not in (None, [], {}):
                    errors.append(f"{loc}.{name}: only applicable when source is 'agent'")

        call_ids: set[str] = set()
        tool_calls = step.get("tool_calls")
        if tool_calls is None:
            tool_calls = []
        elif not isinstance(tool_calls, list):
            errors.append(f"{loc}.tool_calls: must be an array")
            tool_calls = []
        for call_index, call in enumerate(tool_calls):
            cloc = f"{loc}.tool_calls[{call_index}]"
            if not isinstance(call, dict):
                errors.append(f"{cloc}: must be an object")
                continue
            unknown = set(call) - TOOL_CALL_FIELDS
            if unknown:
                errors.append(f"{cloc}: unknown field(s): {sorted(unknown)}")
            for name in ("tool_call_id", "function_name"):
                if not isinstance(call.get(name), str) or not call.get(name):
                    errors.append(f"{cloc}.{name}: required non-empty string")
            if not isinstance(call.get("arguments"), dict):
                errors.append(f"{cloc}.arguments: required object")
            extra = call.get("extra")
            if extra is not None and not isinstance(extra, dict):
                errors.append(f"{cloc}.extra: must be an object")
            if isinstance(call.get("tool_call_id"), str):
                call_ids.add(call["tool_call_id"])

        observation = step.get("observation")
        if observation is not None:
            if not isinstance(observation, dict):
                errors.append(f"{loc}.observation: must be an object")
            else:
                unknown = set(observation) - OBSERVATION_FIELDS
                if unknown:
                    errors.append(f"{loc}.observation: unknown field(s): {sorted(unknown)}")
                results = observation.get("results", [])
                if not isinstance(results, list):
                    errors.append(f"{loc}.observation.results: must be an array")
                    results = []
                for result_index, result in enumerate(results):
                    rloc = f"{loc}.observation.results[{result_index}]"
                    if not isinstance(result, dict):
                        errors.append(f"{rloc}: must be an object")
                        continue
                    unknown = set(result) - OBSERVATION_RESULT_FIELDS
                    if unknown:
                        errors.append(f"{rloc}: unknown field(s): {sorted(unknown)}")
                    source_call_id = result.get("source_call_id")
                    if source_call_id is not None and source_call_id not in call_ids:
                        errors.append(f"{rloc}.source_call_id: unknown tool_call_id {source_call_id!r}")
                    _validate_content_parts(result.get("content"), f"{rloc}.content", errors)
                    extra = result.get("extra")
                    if extra is not None and not isinstance(extra, dict):
                        errors.append(f"{rloc}.extra: must be an object")

        metrics = step.get("metrics")
        if metrics is not None:
            if not isinstance(metrics, dict):
                errors.append(f"{loc}.metrics: must be an object")
            else:
                for name, total_name in (
                    ("prompt_tokens", "total_prompt_tokens"),
                    ("completion_tokens", "total_completion_tokens"),
                    ("cached_tokens", "total_cached_tokens"),
                ):
                    value = metrics.get(name)
                    if value is None:
                        continue
                    if not _is_non_negative_integer(value):
                        errors.append(f"{loc}.metrics.{name}: must be a non-negative integer")
                    else:
                        metric_sums[total_name] += value
                if _is_non_negative_integer(metrics.get("cached_tokens")) and _is_non_negative_integer(metrics.get("prompt_tokens")):
                    if metrics["cached_tokens"] > metrics["prompt_tokens"]:
                        errors.append(f"{loc}.metrics.cached_tokens: cannot exceed prompt_tokens")
                cost = metrics.get("cost_usd")
                if cost is not None:
                    if not _is_non_negative_finite_number(cost):
                        errors.append(f"{loc}.metrics.cost_usd: must be a finite non-negative number")
                    else:
                        saw_cost = True
                        metric_sums["total_cost_usd"] += float(cost)

    final_metrics = data.get("final_metrics")
    if final_metrics is None:
        final_metrics = {}
    elif not isinstance(final_metrics, dict):
        errors.append("trajectory.final_metrics: must be an object")
        final_metrics = {}
    if isinstance(final_metrics, dict):
        total_steps = final_metrics.get("total_steps")
        if total_steps is not None:
            if not _is_non_negative_integer(total_steps):
                errors.append("trajectory.final_metrics.total_steps: must be a non-negative integer")
            elif total_steps != len(steps):
                errors.append(
                    f"trajectory.final_metrics.total_steps: expected {len(steps)}, got {total_steps!r}"
                )
        for name in ("total_prompt_tokens", "total_completion_tokens", "total_cached_tokens"):
            expected = metric_sums[name]
            actual = final_metrics.get(name)
            if actual is not None:
                if not _is_non_negative_integer(actual):
                    errors.append(f"trajectory.final_metrics.{name}: must be a non-negative integer")
                elif actual != expected:
                    errors.append(f"trajectory.final_metrics.{name}: expected sum {expected}, got {actual!r}")
        actual_cost = final_metrics.get("total_cost_usd")
        if actual_cost is not None:
            if not _is_non_negative_finite_number(actual_cost):
                errors.append("trajectory.final_metrics.total_cost_usd: must be a finite non-negative number")
            elif saw_cost and abs(float(actual_cost) - metric_sums["total_cost_usd"]) > 1e-9:
                errors.append(
                    f"trajectory.final_metrics.total_cost_usd: expected sum {metric_sums['total_cost_usd']}, got {actual_cost!r}"
                )

    return errors


def assert_valid_trajectory(trajectory: dict[str, Any] | str | Path) -> None:
    errors = validate_trajectory(trajectory)
    if errors:
        raise ATIFValidationError("\n".join(errors))
