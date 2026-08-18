"""Immutable Gateway Bench experiment schema and route-plan compilation."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import ipaddress
import json
import math
import os
import re
import tomllib
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

try:
    from . import gateway_profiles
except ImportError:  # file-path / Docker mount layout: no package context
    import gateway_profiles


SCHEMA_VERSION = 2
TRACK = "fixed_model_provider"
MODEL_MATCHES = frozenset({"exact_revision", "model_family", "rolling_alias"})
PROVIDER_PROMPT_MODES = frozenset({"provider_default", "isolated_per_call_v1"})
COLD_PREFIX_GATEWAYS = frozenset({"openrouter", "vercel", "concentrate"})
COLD_PREFIX_TRANSFORM_ID = "openbench.responses-prefix.v1"
HARNESS = "pi"
PROTOCOLS = frozenset({"openai_chat", "openai_responses"})
PROTOCOL_ENDPOINT_SUFFIXES = {
    "openai_chat": "/chat/completions",
    "openai_responses": "/responses",
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./@:-]*$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class GatewaySpecError(ValueError):
    """Raised when a Gateway Bench experiment is malformed or unsafe."""


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GatewaySpecError("canonical JSON does not permit non-finite numbers")
        return value
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise GatewaySpecError("canonical JSON object keys must be strings")
        return {key: _canonical_value(item) for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _canonical_value(to_dict())
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON text for public, nonsecret objects."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 hex digest of ``canonical_json(value)``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Window:
    window_id: str
    start: str
    end: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class Budget:
    timeout_s: int
    max_calls: int
    max_output_tokens: int
    usd_cap: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, slots=True)
class Sampling:
    temperature: float | None
    top_p: float | None
    seed: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in dataclasses.asdict(self).items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class InferenceControls:
    reasoning_effort: str
    thinking: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {"reasoning_effort": self.reasoning_effort}
        if self.thinking is not None:
            result["thinking"] = self.thinking
        return result


@dataclass(frozen=True, slots=True)
class Arm:
    arm_id: str
    route_kind: str
    endpoint: str
    protocol: str
    baseline: bool
    canonical_model: str
    requested_model: str
    requested_provider: str
    allowed_models: tuple[str, ...]
    allowed_providers: tuple[str, ...]
    fallback_enabled: bool
    retry_count: int
    cache_enabled: bool
    auth_env: str
    sampling: Sampling
    inference: InferenceControls | None = None
    direct_control_arm_id: str | None = None
    gateway: str | None = None
    gateway_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "arm_id": self.arm_id,
            "route_kind": self.route_kind,
            "endpoint": self.endpoint,
            "protocol": self.protocol,
            "baseline": self.baseline,
            "canonical_model": self.canonical_model,
            "requested_model": self.requested_model,
            "requested_provider": self.requested_provider,
            "allowed_models": list(self.allowed_models),
            "allowed_providers": list(self.allowed_providers),
            "fallback_enabled": self.fallback_enabled,
            "retry_count": self.retry_count,
            "cache_enabled": self.cache_enabled,
            "auth_env": self.auth_env,
            "sampling": self.sampling.to_dict(),
            "direct_control_arm_id": self.direct_control_arm_id,
        }
        if self.inference is not None:
            result["inference"] = self.inference.to_dict()
        if self.gateway is not None:
            result["gateway"] = self.gateway
        if self.gateway_id is not None:
            result["gateway_id"] = self.gateway_id
        return result

    @property
    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class GatewayExperiment:
    schema_version: int
    experiment_id: str
    track: str
    model_match: str
    provider_prompt_mode: str
    harness: str
    tasks: tuple[str, ...]
    repetitions_per_window: int
    schedule_seed: int
    execution_lane: str
    allow_private_endpoint: bool
    private_host_allowlist: tuple[str, ...]
    private_cidr_allowlist: tuple[str, ...]
    windows: tuple[Window, ...]
    budget: Budget
    arms: tuple[Arm, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "track": self.track,
            "model_match": self.model_match,
            "provider_prompt_mode": self.provider_prompt_mode,
            "harness": self.harness,
            "tasks": list(self.tasks),
            "repetitions_per_window": self.repetitions_per_window,
            "schedule_seed": self.schedule_seed,
            "execution_lane": self.execution_lane,
            "allow_private_endpoint": self.allow_private_endpoint,
            "private_host_allowlist": list(self.private_host_allowlist),
            "private_cidr_allowlist": list(self.private_cidr_allowlist),
            "windows": [window.to_dict() for window in self.windows],
            "budget": self.budget.to_dict(),
            "arms": [arm.to_dict() for arm in self.arms],
        }

    @property
    def canonical_json(self) -> str:
        return canonical_json(self)

    @property
    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """Sanitized plan that may be persisted and passed to an adapter."""

    schema_version: int
    experiment_digest: str
    arm_digest: str
    arm_id: str
    route_kind: str
    endpoint: str
    protocol: str
    canonical_model: str
    requested_model: str
    requested_provider: str
    allowed_models: tuple[str, ...]
    allowed_providers: tuple[str, ...]
    fallback_enabled: bool
    retry_count: int
    cache_enabled: bool
    auth_env: str
    sampling: Sampling
    allow_private_endpoint: bool
    private_host_allowlist: tuple[str, ...]
    private_cidr_allowlist: tuple[str, ...]
    inference: InferenceControls | None = None
    # Proxy-only controls. They are bound by arm_digest but intentionally
    # omitted from the adapter-facing route-plan JSON.
    gateway: str | None = None
    gateway_id: str | None = None
    track: str = TRACK
    model_match: str = "exact_revision"
    provider_prompt_mode: str = "provider_default"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "experiment_digest": self.experiment_digest,
            "arm_digest": self.arm_digest,
            "arm_id": self.arm_id,
            "route_kind": self.route_kind,
            "endpoint": self.endpoint,
            "protocol": self.protocol,
            "canonical_model": self.canonical_model,
            "requested_model": self.requested_model,
            "requested_provider": self.requested_provider,
            "allowed_models": list(self.allowed_models),
            "allowed_providers": list(self.allowed_providers),
            "fallback_enabled": self.fallback_enabled,
            "retry_count": self.retry_count,
            "cache_enabled": self.cache_enabled,
            "auth_env": self.auth_env,
            "sampling": self.sampling.to_dict(),
            "allow_private_endpoint": self.allow_private_endpoint,
            "private_host_allowlist": list(self.private_host_allowlist),
            "private_cidr_allowlist": list(self.private_cidr_allowlist),
        }
        if self.inference is not None:
            result["inference"] = self.inference.to_dict()
        return result

    @property
    def canonical_json(self) -> str:
        return canonical_json(self)

    @property
    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True, repr=False, slots=True)
class _ArmSecret:
    arm_id: str
    env_name: str
    value: str = dataclasses.field(repr=False)


@dataclass(frozen=True, repr=False, slots=True)
class SecretPlan:
    """Credential material that must remain in memory and must not be serialized."""

    _secrets: tuple[_ArmSecret, ...]

    def value_for(self, arm_id: str) -> str:
        for secret in self._secrets:
            if secret.arm_id == arm_id:
                return secret.value
        raise KeyError(arm_id)

    def env_name_for(self, arm_id: str) -> str:
        for secret in self._secrets:
            if secret.arm_id == arm_id:
                return secret.env_name
        raise KeyError(arm_id)

    def __repr__(self) -> str:
        arms = ", ".join(secret.arm_id for secret in self._secrets)
        return f"SecretPlan(arms=[{arms}], values=<redacted>)"


_ROOT_FIELDS = {
    "schema_version", "experiment_id", "track", "model_match",
    "provider_prompt_mode", "harness", "tasks",
    "repetitions_per_window", "schedule_seed", "execution_lane",
    "allow_private_endpoint", "private_host_allowlist", "private_cidr_allowlist",
    "windows", "budget", "arms",
}
_WINDOW_FIELDS = {"window_id", "start", "end"}
_BUDGET_FIELDS = {"timeout_s", "max_calls", "max_output_tokens", "usd_cap"}
_SAMPLING_FIELDS = {"temperature", "top_p", "seed"}
_INFERENCE_FIELDS = {"thinking", "reasoning_effort"}
_ARM_FIELDS = {
    "arm_id", "route_kind", "endpoint", "protocol", "baseline",
    "canonical_model", "requested_model", "requested_provider", "allowed_models",
    "allowed_providers", "fallback_enabled", "retry_count", "cache_enabled",
    "auth_env", "sampling", "inference", "direct_control_arm_id", "gateway",
    "gateway_id",
}


def _table(value: Any, path: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GatewaySpecError(f"{path} must be a TOML table")
    unknown = sorted(set(value) - fields)
    if unknown:
        noun = "field" if len(unknown) == 1 else "fields"
        raise GatewaySpecError(f"{path} has unknown {noun}: {', '.join(unknown)}")
    return value


def _required(table: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in table:
        raise GatewaySpecError(f"{path} missing required field: {key}")
    return table[key]


def _string(value: Any, path: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewaySpecError(f"{path} must be a non-empty string")
    result = value.strip()
    if pattern is not None and not pattern.fullmatch(result):
        raise GatewaySpecError(f"{path} has invalid format: {result!r}")
    return result


def _integer(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewaySpecError(f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise GatewaySpecError(f"{path} must be at least {minimum}")
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise GatewaySpecError(f"{path} must be a boolean")
    return value


def _string_tuple(value: Any, path: str, *, pattern: re.Pattern[str] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GatewaySpecError(f"{path} must be a non-empty array")
    result = tuple(_string(item, f"{path}[{index}]", pattern)
                   for index, item in enumerate(value))
    if len(set(result)) != len(result):
        raise GatewaySpecError(f"{path} must not contain duplicates")
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _positive_decimal(value: Any, path: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise GatewaySpecError(f"{path} must be a positive decimal")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise GatewaySpecError(f"{path} must be a positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise GatewaySpecError(f"{path} must be greater than zero")
    return _decimal_text(parsed)


def _timestamp(value: Any, path: str) -> tuple[str, dt.datetime]:
    text = _string(value, path)
    if not _RFC3339_RE.fullmatch(text):
        raise GatewaySpecError(f"{path} must be an RFC3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatewaySpecError(f"{path} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GatewaySpecError(f"{path} must include a UTC offset")
    utc = parsed.astimezone(dt.timezone.utc)
    normalized = utc.isoformat().replace("+00:00", "Z")
    return normalized, utc


def _parse_windows(value: Any) -> tuple[Window, ...]:
    if not isinstance(value, list) or not value:
        raise GatewaySpecError("windows must be a non-empty array of tables")
    parsed: list[tuple[Window, dt.datetime, dt.datetime]] = []
    ids: set[str] = set()
    for index, raw in enumerate(value):
        path = f"windows[{index}]"
        table = _table(raw, path, _WINDOW_FIELDS)
        window_id = _string(_required(table, "window_id", path),
                            f"{path}.window_id", _ID_RE)
        if window_id in ids:
            raise GatewaySpecError(f"windows has duplicate window_id: {window_id}")
        ids.add(window_id)
        start_text, start = _timestamp(_required(table, "start", path), f"{path}.start")
        end_text, end = _timestamp(_required(table, "end", path), f"{path}.end")
        if start >= end:
            raise GatewaySpecError(f"{path} start must be before end")
        parsed.append((Window(window_id, start_text, end_text), start, end))
    ordered = sorted(parsed, key=lambda item: item[1])
    for previous, current in zip(ordered, ordered[1:]):
        if current[1] < previous[2]:
            raise GatewaySpecError(
                f"windows overlap: {previous[0].window_id} and {current[0].window_id}"
            )
    return tuple(item[0] for item in parsed)


def _parse_budget(value: Any) -> Budget:
    table = _table(value, "budget", _BUDGET_FIELDS)
    return Budget(
        timeout_s=_integer(_required(table, "timeout_s", "budget"),
                           "budget.timeout_s", minimum=1),
        max_calls=_integer(_required(table, "max_calls", "budget"),
                           "budget.max_calls", minimum=1),
        max_output_tokens=_integer(_required(table, "max_output_tokens", "budget"),
                                   "budget.max_output_tokens", minimum=1),
        usd_cap=_positive_decimal(_required(table, "usd_cap", "budget"),
                                  "budget.usd_cap"),
    )


def _number(value: Any, path: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatewaySpecError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise GatewaySpecError(f"{path} must be between {minimum} and {maximum}")
    return result


def _parse_sampling(value: Any, path: str) -> Sampling:
    table = _table(value, path, _SAMPLING_FIELDS)
    if not table:
        return Sampling(temperature=None, top_p=None, seed=None)
    if set(table) != _SAMPLING_FIELDS:
        raise GatewaySpecError(
            f"{path} must be empty or contain temperature, top_p, and seed"
        )
    return Sampling(
        temperature=_number(_required(table, "temperature", path),
                            f"{path}.temperature", 0.0, 2.0),
        top_p=_number(_required(table, "top_p", path),
                      f"{path}.top_p", 0.0, 1.0),
        seed=_integer(_required(table, "seed", path), f"{path}.seed", minimum=0),
    )


def _parse_inference(value: Any, path: str) -> InferenceControls:
    table = _table(value, path, _INFERENCE_FIELDS)
    thinking = table.get("thinking")
    if thinking is not None:
        thinking = _string(thinking, f"{path}.thinking")
        if thinking != "enabled":
            raise GatewaySpecError(f"{path}.thinking must be 'enabled'")
    effort = _string(
        _required(table, "reasoning_effort", path),
        f"{path}.reasoning_effort",
    )
    efforts = {"none", "low", "medium", "high", "xhigh", "max"}
    if effort not in efforts:
        raise GatewaySpecError(
            f"{path}.reasoning_effort must be one of: "
            + ", ".join(sorted(efforts))
        )
    return InferenceControls(reasoning_effort=effort, thinking=thinking)


def _validate_endpoint(
    value: Any,
    path: str,
    allow_private_endpoint: bool,
    private_hosts: tuple[str, ...],
    private_cidrs: tuple[str, ...],
) -> str:
    endpoint = _string(value, path)
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise GatewaySpecError(f"{path} must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise GatewaySpecError(f"{path} must not contain credentials, query, or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise GatewaySpecError(f"{path} has an invalid port") from exc
    _validate_endpoint_target(
        parsed.hostname,
        path,
        allow_private_endpoint,
        private_hosts,
        private_cidrs,
    )
    return endpoint


def _validate_endpoint_target(
    hostname_value: str,
    path: str,
    allow_private_endpoint: bool,
    private_hosts: tuple[str, ...],
    private_cidrs: tuple[str, ...],
) -> None:
    """Require private-mode destinations to match an explicit host or CIDR."""
    hostname = _normalize_hostname(hostname_value, path, allow_ip=True)
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if allow_private_endpoint:
        networks = tuple(ipaddress.ip_network(cidr) for cidr in private_cidrs)
        if (
            hostname not in private_hosts
            and (
                address is None
                or not any(address in network for network in networks)
            )
        ):
            raise GatewaySpecError(
                f"{path} host is not covered by an explicit endpoint allowlist"
            )
        return
    if address is None:
        if (hostname == "localhost" or hostname.endswith(".localhost")
                or "." not in hostname or hostname.isdigit()):
            raise GatewaySpecError(
                f"{path} is not a public hostname; set allow_private_endpoint and allow it explicitly"
            )
    elif not address.is_global:
        raise GatewaySpecError(
            f"{path} targets a non-public address; set allow_private_endpoint with allowlists"
        )


def _parse_arm(
    value: Any,
    index: int,
    allow_private_endpoint: bool,
    private_hosts: tuple[str, ...],
    private_cidrs: tuple[str, ...],
    *,
    allow_responses_inference: bool,
) -> Arm:
    path = f"arms[{index}]"
    table = _table(value, path, _ARM_FIELDS)
    route_kind = _string(_required(table, "route_kind", path), f"{path}.route_kind")
    if route_kind not in {"direct", "gateway"}:
        raise GatewaySpecError(f"{path}.route_kind must be 'direct' or 'gateway'")
    protocol = _string(_required(table, "protocol", path), f"{path}.protocol")
    if protocol not in PROTOCOLS:
        raise GatewaySpecError(
            f"{path}.protocol must be one of: {', '.join(sorted(PROTOCOLS))}"
        )
    endpoint = _validate_endpoint(
        _required(table, "endpoint", path),
        f"{path}.endpoint",
        allow_private_endpoint,
        private_hosts,
        private_cidrs,
    )
    suffix = PROTOCOL_ENDPOINT_SUFFIXES[protocol]
    if not urllib.parse.urlsplit(endpoint).path.rstrip("/").endswith(suffix):
        raise GatewaySpecError(f"{path}.endpoint path must end with {suffix}")
    direct_control = table.get("direct_control_arm_id")
    if direct_control is not None:
        direct_control = _string(direct_control, f"{path}.direct_control_arm_id", _ID_RE)
    gateway = table.get("gateway")
    if gateway is not None:
        gateway = _string(gateway, f"{path}.gateway")
    gateway_id = table.get("gateway_id")
    if gateway_id is not None:
        gateway_id = _string(gateway_id, f"{path}.gateway_id", _ID_RE)
    inference = table.get("inference")
    if inference is not None:
        inference = _parse_inference(inference, f"{path}.inference")
        if protocol == "openai_responses" and inference.thinking is not None:
            raise GatewaySpecError(
                f"{path}.inference.thinking is supported only for openai_chat"
            )
        if protocol == "openai_chat" and inference.thinking is None:
            raise GatewaySpecError(
                f"{path}.inference.thinking is required for openai_chat"
            )
        if protocol == "openai_responses" and not allow_responses_inference:
            raise GatewaySpecError(
                f"{path}.inference for openai_responses is supported only by Gateway Probe"
            )
    arm = Arm(
        arm_id=_string(_required(table, "arm_id", path), f"{path}.arm_id", _ID_RE),
        route_kind=route_kind,
        endpoint=endpoint,
        protocol=protocol,
        baseline=_boolean(_required(table, "baseline", path), f"{path}.baseline"),
        canonical_model=_string(_required(table, "canonical_model", path),
                                f"{path}.canonical_model"),
        requested_model=_string(_required(table, "requested_model", path),
                                f"{path}.requested_model"),
        requested_provider=_string(_required(table, "requested_provider", path),
                                   f"{path}.requested_provider", _ID_RE),
        allowed_models=_string_tuple(_required(table, "allowed_models", path),
                                     f"{path}.allowed_models"),
        allowed_providers=_string_tuple(_required(table, "allowed_providers", path),
                                        f"{path}.allowed_providers", pattern=_ID_RE),
        fallback_enabled=_boolean(_required(table, "fallback_enabled", path),
                                  f"{path}.fallback_enabled"),
        retry_count=_integer(_required(table, "retry_count", path),
                             f"{path}.retry_count", minimum=0),
        cache_enabled=_boolean(_required(table, "cache_enabled", path),
                               f"{path}.cache_enabled"),
        auth_env=_string(_required(table, "auth_env", path), f"{path}.auth_env", _ENV_RE),
        sampling=_parse_sampling(_required(table, "sampling", path), f"{path}.sampling"),
        inference=inference,
        direct_control_arm_id=direct_control,
        gateway=gateway,
        gateway_id=gateway_id,
    )
    if arm.requested_model not in arm.allowed_models:
        raise GatewaySpecError(f"{path}.allowed_models must contain requested_model")
    if arm.requested_provider not in arm.allowed_providers:
        raise GatewaySpecError(f"{path}.allowed_providers must contain requested_provider")
    if arm.allowed_providers != (arm.requested_provider,):
        raise GatewaySpecError(
            f"{path}.allowed_providers must contain only requested_provider"
        )
    if arm.route_kind == "direct" and arm.direct_control_arm_id is not None:
        raise GatewaySpecError(f"{path}: direct arm cannot set direct_control_arm_id")
    if (
        arm.route_kind == "gateway"
        and arm.direct_control_arm_id is None
    ):
        raise GatewaySpecError(f"{path}: gateway arm requires direct_control_arm_id")
    if arm.route_kind == "direct" and "gateway" in table:
        raise GatewaySpecError(f"{path}: direct arm must not declare gateway")
    if arm.route_kind == "direct" and "gateway_id" in table:
        raise GatewaySpecError(f"{path}: direct arm must not declare gateway_id")
    try:
        gateway_profiles.validate_arm(
            route_kind=arm.route_kind,
            gateway=arm.gateway,
            gateway_id=arm.gateway_id,
            endpoint=arm.endpoint,
            protocol=arm.protocol,
            requested_model=arm.requested_model,
            requested_provider=arm.requested_provider,
            allow_private_endpoint=allow_private_endpoint,
        )
    except gateway_profiles.GatewayProfileError as exc:
        raise GatewaySpecError(f"{path}: {exc}") from exc
    if arm.retry_count != 0:
        raise GatewaySpecError(f"{path}.retry_count must be 0")
    if arm.cache_enabled:
        raise GatewaySpecError(f"{path}.cache_enabled must be false")
    if arm.fallback_enabled:
        raise GatewaySpecError(
            f"{path}.fallback_enabled must be false for {TRACK}"
        )
    return arm


def _validate_gateway_controls(arms: tuple[Arm, ...]) -> None:
    by_id = {arm.arm_id: arm for arm in arms}
    if len(by_id) != len(arms):
        raise GatewaySpecError("arms must have unique arm_id values")
    baselines = [arm for arm in arms if arm.baseline]
    if len(baselines) != 1 or baselines[0].route_kind != "direct":
        raise GatewaySpecError(f"{TRACK} requires exactly one baseline direct arm")
    if not any(arm.route_kind == "gateway" for arm in arms):
        raise GatewaySpecError(f"{TRACK} requires at least one gateway arm")
    for arm in arms:
        if arm.route_kind == "direct" and not arm.baseline:
            raise GatewaySpecError(f"{TRACK} direct arm must be the baseline")
        if arm.route_kind != "gateway":
            continue
        control = by_id.get(arm.direct_control_arm_id)
        if control is None or control.route_kind != "direct":
            raise GatewaySpecError(
                f"arm {arm.arm_id!r} references unknown direct control "
                f"{arm.direct_control_arm_id!r}"
            )
        comparable = (
            "protocol", "canonical_model", "requested_provider",
            "allowed_providers", "sampling", "inference",
        )
        for field in comparable:
            if getattr(arm, field) != getattr(control, field):
                raise GatewaySpecError(
                    f"arm {arm.arm_id!r} {field} must match direct control {control.arm_id!r}"
                )

def _parse_allowlists(table: Mapping[str, Any], allow_private_endpoint: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    hosts_raw = table.get("private_host_allowlist", [])
    cidrs_raw = table.get("private_cidr_allowlist", [])
    if not isinstance(hosts_raw, list) or any(not isinstance(item, str) for item in hosts_raw):
        raise GatewaySpecError("private_host_allowlist must be an array of hostnames")
    hosts = tuple(
        _normalize_hostname(item, f"private_host_allowlist[{index}]")
        for index, item in enumerate(hosts_raw)
    )
    if len(set(hosts)) != len(hosts):
        raise GatewaySpecError("private_host_allowlist must not contain duplicates")
    if not isinstance(cidrs_raw, list) or any(not isinstance(item, str) for item in cidrs_raw):
        raise GatewaySpecError("private_cidr_allowlist must be an array of CIDRs")
    cidrs: list[str] = []
    for item in cidrs_raw:
        try:
            cidrs.append(str(ipaddress.ip_network(item, strict=True)))
        except ValueError as exc:
            raise GatewaySpecError(f"invalid private_cidr_allowlist entry: {item!r}") from exc
    if len(set(cidrs)) != len(cidrs):
        raise GatewaySpecError("private_cidr_allowlist must not contain duplicates")
    if allow_private_endpoint and not (hosts or cidrs):
        raise GatewaySpecError("allow_private_endpoint=true requires a hostname or CIDR allowlist")
    if not allow_private_endpoint and (hosts or cidrs):
        raise GatewaySpecError("private endpoint allowlists require allow_private_endpoint=true")
    return hosts, tuple(cidrs)


def parse_endpoint_allowlists(
    data: Mapping[str, Any],
    allow_private_endpoint: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate endpoint allowlists for another gateway experiment surface."""
    return _parse_allowlists(data, allow_private_endpoint)


def _normalize_hostname(value: str, path: str, *, allow_ip: bool = False) -> str:
    hostname = value.strip().rstrip(".").lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if allow_ip:
            return hostname
        raise GatewaySpecError(f"{path} must use private_cidr_allowlist for IP addresses")
    labels = hostname.split(".")
    if (not hostname or len(hostname) > 253 or hostname.isdigit()
            or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels)):
        raise GatewaySpecError(f"{path} must be a bare DNS hostname")
    return hostname


def parse_route_arm(
    value: Any,
    index: int,
    *,
    allow_private_endpoint: bool,
    private_host_allowlist: tuple[str, ...],
    private_cidr_allowlist: tuple[str, ...],
) -> Arm:
    """Parse one route arm without imposing a benchmark scheduling contract."""
    return _parse_arm(
        value,
        index,
        allow_private_endpoint,
        private_host_allowlist,
        private_cidr_allowlist,
        allow_responses_inference=True,
    )


def validate_fixed_route_arms(arms: tuple[Arm, ...]) -> None:
    """Require the fixed direct-control comparison used by gateway tracks."""
    _validate_gateway_controls(arms)


def parse_experiment(data: Mapping[str, Any]) -> GatewayExperiment:
    """Validate an already-decoded TOML experiment mapping."""
    table = _table(data, "experiment", _ROOT_FIELDS)
    schema_version = _integer(_required(table, "schema_version", "experiment"),
                              "schema_version", minimum=1)
    if schema_version != SCHEMA_VERSION:
        raise GatewaySpecError(f"schema_version must be {SCHEMA_VERSION}")
    track = _string(_required(table, "track", "experiment"), "track")
    if track != TRACK:
        raise GatewaySpecError(f"track must be {TRACK!r}")
    model_match = _string(table.get("model_match", "exact_revision"), "model_match")
    if model_match not in MODEL_MATCHES:
        raise GatewaySpecError(
            f"model_match must be one of: {', '.join(sorted(MODEL_MATCHES))}"
        )
    provider_prompt_mode = _string(
        _required(table, "provider_prompt_mode", "experiment"),
        "provider_prompt_mode",
    )
    if provider_prompt_mode not in PROVIDER_PROMPT_MODES:
        raise GatewaySpecError(
            "provider_prompt_mode must be one of: "
            + ", ".join(sorted(PROVIDER_PROMPT_MODES))
        )
    harness = _string(_required(table, "harness", "experiment"), "harness")
    if harness != HARNESS:
        raise GatewaySpecError(f"harness must be {HARNESS!r} for the MVP")
    allow_private_endpoint = _boolean(table.get("allow_private_endpoint", False), "allow_private_endpoint")
    hosts, cidrs = _parse_allowlists(table, allow_private_endpoint)
    tasks = _string_tuple(_required(table, "tasks", "experiment"), "tasks", pattern=_TASK_RE)
    if any(".." in task.split("/") for task in tasks):
        raise GatewaySpecError("tasks must not contain '..' path segments")
    raw_arms = _required(table, "arms", "experiment")
    if not isinstance(raw_arms, list) or len(raw_arms) < 2:
        raise GatewaySpecError("arms must contain at least two arm tables")
    arms = tuple(
        _parse_arm(
            raw,
            index,
            allow_private_endpoint,
            hosts,
            cidrs,
            allow_responses_inference=False,
        )
        for index, raw in enumerate(raw_arms)
    )
    _validate_gateway_controls(arms)
    if any(
        arm.sampling.temperature is None
        or arm.sampling.top_p is None
        or arm.sampling.seed is None
        for arm in arms
    ):
        raise GatewaySpecError(
            "gateway_bench arms must configure temperature, top_p, and seed"
        )
    if provider_prompt_mode == "isolated_per_call_v1" and any(
        arm.protocol != "openai_responses" for arm in arms
    ):
        raise GatewaySpecError(
            "provider_prompt_mode='isolated_per_call_v1' requires "
            "openai_responses for every arm"
        )
    if provider_prompt_mode == "isolated_per_call_v1" and any(
        arm.route_kind == "gateway" and arm.gateway not in COLD_PREFIX_GATEWAYS
        for arm in arms
    ):
        raise GatewaySpecError(
            "provider_prompt_mode='isolated_per_call_v1' is admitted only for "
            "OpenRouter, Vercel, and Concentrate gateway arms"
        )
    if provider_prompt_mode == "isolated_per_call_v1" and any(
        arm.route_kind == "direct"
        and (
            arm.requested_provider.casefold() != "openai"
            or urllib.parse.urlsplit(arm.endpoint).hostname != "api.openai.com"
        )
        for arm in arms
    ):
        raise GatewaySpecError(
            "provider_prompt_mode='isolated_per_call_v1' requires the direct "
            "OpenAI API arm"
        )
    lane = _string(_required(table, "execution_lane", "experiment"), "execution_lane")
    if lane not in {"local", "docker"}:
        raise GatewaySpecError("execution_lane must be 'local' or 'docker'")
    return GatewayExperiment(
        schema_version=schema_version,
        experiment_id=_string(_required(table, "experiment_id", "experiment"),
                              "experiment_id", _ID_RE),
        track=track,
        model_match=model_match,
        provider_prompt_mode=provider_prompt_mode,
        harness=harness,
        tasks=tasks,
        repetitions_per_window=_integer(
            _required(table, "repetitions_per_window", "experiment"),
            "repetitions_per_window", minimum=1,
        ),
        schedule_seed=_integer(_required(table, "schedule_seed", "experiment"),
                               "schedule_seed", minimum=0),
        execution_lane=lane,
        allow_private_endpoint=allow_private_endpoint,
        private_host_allowlist=hosts,
        private_cidr_allowlist=cidrs,
        windows=_parse_windows(_required(table, "windows", "experiment")),
        budget=_parse_budget(_required(table, "budget", "experiment")),
        arms=arms,
    )


def parse_experiment_toml(text: str, *, source: str = "<string>") -> GatewayExperiment:
    """Decode and validate Gateway Bench TOML text."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise GatewaySpecError(f"invalid Gateway Bench TOML in {source}: {exc}") from exc
    try:
        return parse_experiment(raw)
    except GatewaySpecError as exc:
        raise GatewaySpecError(f"invalid Gateway Bench experiment in {source}: {exc}") from exc


def load_experiment(path: str | os.PathLike[str]) -> GatewayExperiment:
    """Load and validate a Gateway Bench experiment TOML file."""
    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GatewaySpecError(f"cannot read Gateway Bench experiment {resolved}: {exc}") from exc
    return parse_experiment_toml(text, source=str(resolved))


def compile_fixed_route_plans(
    *,
    arms: tuple[Arm, ...],
    experiment_digest: str,
    track: str,
    model_match: str,
    provider_prompt_mode: str,
    allow_private_endpoint: bool,
    private_host_allowlist: tuple[str, ...],
    private_cidr_allowlist: tuple[str, ...],
    environ: Mapping[str, str],
    admitted_auth_envs: set[str] | frozenset[str],
) -> tuple[tuple[RoutePlan, ...], SecretPlan]:
    """Compile one validated direct-control route set for any gateway track."""
    if not isinstance(experiment_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", experiment_digest
    ):
        raise GatewaySpecError("experiment_digest must be a lowercase SHA-256 digest")
    if not isinstance(track, str) or not track:
        raise GatewaySpecError("track must be a non-empty string")
    if model_match not in MODEL_MATCHES:
        raise GatewaySpecError("model_match is unsupported")
    if provider_prompt_mode not in PROVIDER_PROMPT_MODES:
        raise GatewaySpecError("provider_prompt_mode is unsupported")
    if not isinstance(allow_private_endpoint, bool):
        raise GatewaySpecError("allow_private_endpoint must be a boolean")
    if (
        not isinstance(private_host_allowlist, tuple)
        or any(not isinstance(item, str) for item in private_host_allowlist)
        or not isinstance(private_cidr_allowlist, tuple)
        or any(not isinstance(item, str) for item in private_cidr_allowlist)
    ):
        raise GatewaySpecError("private endpoint allowlists must be tuples of strings")
    policy = {
        "private_host_allowlist": list(private_host_allowlist),
        "private_cidr_allowlist": list(private_cidr_allowlist),
    }
    hosts, cidrs = _parse_allowlists(policy, allow_private_endpoint)
    if not isinstance(arms, tuple) or not arms or any(
        not isinstance(arm, Arm) for arm in arms
    ):
        raise GatewaySpecError("arms must be a non-empty tuple of Arm values")
    validated_arms = tuple(
        _parse_arm(
            arm.to_dict(),
            index,
            allow_private_endpoint,
            hosts,
            cidrs,
            allow_responses_inference=track == "request_probe",
        )
        for index, arm in enumerate(arms)
    )
    _validate_gateway_controls(validated_arms)
    admitted = frozenset(admitted_auth_envs)
    if any(not isinstance(name, str) for name in admitted):
        raise GatewaySpecError("admitted_auth_envs must contain environment variable names")
    declared = frozenset(arm.auth_env for arm in validated_arms)
    unexpected = sorted(admitted - declared)
    if unexpected:
        raise GatewaySpecError(
            f"admitted_auth_envs contains undeclared names: {', '.join(unexpected)}"
        )
    plans: list[RoutePlan] = []
    secrets: list[_ArmSecret] = []
    for arm in validated_arms:
        if arm.auth_env not in admitted:
            raise GatewaySpecError(
                f"auth environment variable {arm.auth_env!r} is not explicitly admitted"
            )
        value = environ.get(arm.auth_env)
        if not isinstance(value, str) or not value:
            raise GatewaySpecError(
                f"auth environment variable {arm.auth_env!r} is missing or empty"
            )
        plans.append(RoutePlan(
            schema_version=SCHEMA_VERSION,
            experiment_digest=experiment_digest,
            track=track,
            model_match=model_match,
            provider_prompt_mode=provider_prompt_mode,
            arm_digest=arm.digest,
            arm_id=arm.arm_id,
            route_kind=arm.route_kind,
            endpoint=arm.endpoint,
            protocol=arm.protocol,
            canonical_model=arm.canonical_model,
            requested_model=arm.requested_model,
            requested_provider=arm.requested_provider,
            allowed_models=arm.allowed_models,
            allowed_providers=arm.allowed_providers,
            fallback_enabled=arm.fallback_enabled,
            retry_count=arm.retry_count,
            cache_enabled=arm.cache_enabled,
            auth_env=arm.auth_env,
            sampling=arm.sampling,
            inference=arm.inference,
            allow_private_endpoint=allow_private_endpoint,
            private_host_allowlist=hosts,
            private_cidr_allowlist=cidrs,
            gateway=arm.gateway,
            gateway_id=arm.gateway_id,
        ))
        secrets.append(_ArmSecret(arm.arm_id, arm.auth_env, value))
    return tuple(plans), SecretPlan(tuple(secrets))


def compile_route_plans(
    experiment: GatewayExperiment,
    *,
    environ: Mapping[str, str],
    admitted_auth_envs: set[str] | frozenset[str],
) -> tuple[tuple[RoutePlan, ...], SecretPlan]:
    """Compile sanitized route plans and admitted in-memory credentials."""
    if not isinstance(experiment, GatewayExperiment):
        raise TypeError("experiment must be a GatewayExperiment")
    # Public dataclasses are convenient value types, but parsing remains the
    # validation boundary. Reparse here so hand-built instances cannot bypass it.
    experiment = parse_experiment(experiment.to_dict())
    return compile_fixed_route_plans(
        arms=experiment.arms,
        experiment_digest=experiment.digest,
        track=experiment.track,
        model_match=experiment.model_match,
        provider_prompt_mode=experiment.provider_prompt_mode,
        allow_private_endpoint=experiment.allow_private_endpoint,
        private_host_allowlist=experiment.private_host_allowlist,
        private_cidr_allowlist=experiment.private_cidr_allowlist,
        environ=environ,
        admitted_auth_envs=admitted_auth_envs,
    )


# Explicit aliases make call sites readable while retaining one parser contract.
load_gateway_experiment = load_experiment
parse_gateway_experiment = parse_experiment
