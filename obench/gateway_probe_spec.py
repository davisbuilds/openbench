"""Immutable request-level Gateway Probe experiment schema."""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import gateway_spec


SCHEMA_VERSION = 1
TRACK = "request_probe"
CONDITIONS = ("cold", "warm")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ROOT_FIELDS = {
    "schema_version", "experiment_id", "track", "model_match",
    "repetitions", "schedule_seed", "allow_private_endpoint",
    "private_host_allowlist", "private_cidr_allowlist", "budget",
    "cases", "arms",
}
_BUDGET_FIELDS = {
    "timeout_s",
    "max_output_tokens",
    "usd_cap",
    "max_total_attempts",
}
_CASE_FIELDS = {"case_id", "prompt"}


class GatewayProbeSpecError(ValueError):
    """Raised when a Gateway Probe experiment is malformed or unsafe."""


def _table(value: Any, path: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GatewayProbeSpecError(f"{path} must be a table")
    unknown = sorted(set(value) - fields)
    if unknown:
        raise GatewayProbeSpecError(f"{path} has unknown field: {unknown[0]}")
    return value


def _required(value: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in value:
        raise GatewayProbeSpecError(f"{path} is missing {key}")
    return value[key]


def _string(value: Any, path: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise GatewayProbeSpecError(f"{path} must be a non-empty string")
    if pattern is not None and not pattern.fullmatch(value):
        raise GatewayProbeSpecError(f"{path} has invalid characters")
    return value


def _integer(value: Any, path: str, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise GatewayProbeSpecError(f"{path} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class ProbeBudget:
    timeout_s: int
    max_output_tokens: int
    usd_cap: str
    max_total_attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        value = {
            "timeout_s": self.timeout_s,
            "max_output_tokens": self.max_output_tokens,
            "usd_cap": self.usd_cap,
        }
        if self.max_total_attempts != 1:
            value["max_total_attempts"] = self.max_total_attempts
        return value


@dataclass(frozen=True, slots=True)
class ProbeCase:
    case_id: str
    prompt: str

    @property
    def prompt_digest(self) -> str:
        return gateway_spec.canonical_digest({"prompt": self.prompt})

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "prompt": self.prompt,
            "prompt_digest": self.prompt_digest,
        }


@dataclass(frozen=True, slots=True)
class GatewayProbeExperiment:
    schema_version: int
    experiment_id: str
    track: str
    model_match: str
    repetitions: int
    schedule_seed: int
    allow_private_endpoint: bool
    private_host_allowlist: tuple[str, ...]
    private_cidr_allowlist: tuple[str, ...]
    budget: ProbeBudget
    cases: tuple[ProbeCase, ...]
    arms: tuple[gateway_spec.Arm, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "track": self.track,
            "model_match": self.model_match,
            "repetitions": self.repetitions,
            "schedule_seed": self.schedule_seed,
            "allow_private_endpoint": self.allow_private_endpoint,
            "private_host_allowlist": list(self.private_host_allowlist),
            "private_cidr_allowlist": list(self.private_cidr_allowlist),
            "budget": self.budget.to_dict(),
            "cases": [case.to_dict() for case in self.cases],
            "arms": [arm.to_dict() for arm in self.arms],
        }

    @property
    def digest(self) -> str:
        return gateway_spec.canonical_digest(self.to_dict())


def _parse_budget(value: Any) -> ProbeBudget:
    table = _table(value, "budget", _BUDGET_FIELDS)
    raw_cap = _required(table, "usd_cap", "budget")
    if isinstance(raw_cap, bool) or not isinstance(raw_cap, (str, int, float)):
        raise GatewayProbeSpecError("budget.usd_cap must be a positive decimal")
    try:
        cap = Decimal(str(raw_cap))
    except InvalidOperation as exc:
        raise GatewayProbeSpecError("budget.usd_cap must be a positive decimal") from exc
    if not cap.is_finite() or cap <= 0:
        raise GatewayProbeSpecError("budget.usd_cap must be a positive decimal")
    return ProbeBudget(
        timeout_s=_integer(_required(table, "timeout_s", "budget"), "budget.timeout_s", 1),
        max_output_tokens=_integer(
            _required(table, "max_output_tokens", "budget"),
            "budget.max_output_tokens",
            1,
        ),
        usd_cap=str(cap),
        max_total_attempts=_integer(
            table.get("max_total_attempts", 1),
            "budget.max_total_attempts",
            1,
        ),
    )


def _parse_cases(value: Any) -> tuple[ProbeCase, ...]:
    if not isinstance(value, list) or not value:
        raise GatewayProbeSpecError("cases must contain at least one case table")
    cases = []
    for index, raw in enumerate(value):
        path = f"cases[{index}]"
        table = _table(raw, path, _CASE_FIELDS)
        cases.append(ProbeCase(
            case_id=_string(_required(table, "case_id", path), f"{path}.case_id", _ID_RE),
            prompt=_string(_required(table, "prompt", path), f"{path}.prompt"),
        ))
    if len({case.case_id for case in cases}) != len(cases):
        raise GatewayProbeSpecError("cases must have unique case_id values")
    return tuple(cases)


def parse_experiment(data: Mapping[str, Any]) -> GatewayProbeExperiment:
    table = _table(dict(data), "experiment", _ROOT_FIELDS)
    version = _integer(
        _required(table, "schema_version", "experiment"), "schema_version", 1
    )
    if version != SCHEMA_VERSION:
        raise GatewayProbeSpecError(f"schema_version must be {SCHEMA_VERSION}")
    track = _string(_required(table, "track", "experiment"), "track")
    if track != TRACK:
        raise GatewayProbeSpecError(f"track must be {TRACK!r}")
    model_match = _string(table.get("model_match", "exact_revision"), "model_match")
    if model_match not in gateway_spec.MODEL_MATCHES:
        raise GatewayProbeSpecError("model_match is unsupported")
    allow_private = table.get("allow_private_endpoint", False)
    if not isinstance(allow_private, bool):
        raise GatewayProbeSpecError("allow_private_endpoint must be a boolean")
    try:
        hosts, cidrs = gateway_spec.parse_endpoint_allowlists(table, allow_private)
        raw_arms = _required(table, "arms", "experiment")
        if not isinstance(raw_arms, list) or len(raw_arms) < 2:
            raise GatewayProbeSpecError("arms must contain at least two arm tables")
        arms = tuple(
            gateway_spec.parse_route_arm(
                raw,
                index,
                allow_private_endpoint=allow_private,
                private_host_allowlist=hosts,
                private_cidr_allowlist=cidrs,
            )
            for index, raw in enumerate(raw_arms)
        )
        gateway_spec.validate_fixed_route_arms(arms)
    except gateway_spec.GatewaySpecError as exc:
        raise GatewayProbeSpecError(str(exc)) from exc
    protocols = {arm.protocol for arm in arms}
    if len(protocols) != 1:
        raise GatewayProbeSpecError("all arms must use the same protocol")
    return GatewayProbeExperiment(
        schema_version=version,
        experiment_id=_string(
            _required(table, "experiment_id", "experiment"), "experiment_id", _ID_RE
        ),
        track=track,
        model_match=model_match,
        repetitions=_integer(
            _required(table, "repetitions", "experiment"), "repetitions", 1
        ),
        schedule_seed=_integer(
            _required(table, "schedule_seed", "experiment"), "schedule_seed", 0
        ),
        allow_private_endpoint=allow_private,
        private_host_allowlist=hosts,
        private_cidr_allowlist=cidrs,
        budget=_parse_budget(_required(table, "budget", "experiment")),
        cases=_parse_cases(_required(table, "cases", "experiment")),
        arms=arms,
    )


def parse_experiment_toml(text: str, *, source: str = "<string>") -> GatewayProbeExperiment:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise GatewayProbeSpecError(f"invalid Gateway Probe TOML in {source}: {exc}") from exc
    try:
        return parse_experiment(raw)
    except GatewayProbeSpecError as exc:
        raise GatewayProbeSpecError(f"invalid Gateway Probe experiment in {source}: {exc}") from exc


def load_experiment(path: str | os.PathLike[str]) -> GatewayProbeExperiment:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GatewayProbeSpecError(f"cannot read Gateway Probe experiment {source}: {exc}") from exc
    return parse_experiment_toml(text, source=str(source))


def compile_route_plans(
    experiment: GatewayProbeExperiment,
    *,
    environ: Mapping[str, str],
    admitted_auth_envs: set[str] | frozenset[str],
) -> tuple[tuple[gateway_spec.RoutePlan, ...], gateway_spec.SecretPlan]:
    if not isinstance(experiment, GatewayProbeExperiment):
        raise TypeError("experiment must be a GatewayProbeExperiment")
    source = experiment.to_dict()
    for case in source["cases"]:
        case.pop("prompt_digest")
    experiment = parse_experiment(source)
    return gateway_spec.compile_fixed_route_plans(
        arms=experiment.arms,
        experiment_digest=experiment.digest,
        track=TRACK,
        model_match=experiment.model_match,
        provider_prompt_mode="provider_default",
        allow_private_endpoint=experiment.allow_private_endpoint,
        private_host_allowlist=experiment.private_host_allowlist,
        private_cidr_allowlist=experiment.private_cidr_allowlist,
        environ=environ,
        admitted_auth_envs=admitted_auth_envs,
    )
