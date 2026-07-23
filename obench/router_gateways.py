"""Strict Gateway Tax profiles for request shaping and route evidence."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any


GATEWAYS = frozenset({"openrouter", "vercel"})
CONCENTRATE_UNSUPPORTED = (
    "strict Gateway Tax is unsupported for concentrate because its current "
    "Chat Completions contract cannot disable or prove fallback, retries, and caching"
)
CLOUDFLARE_UNSUPPORTED = (
    "strict Gateway Tax is unsupported for cloudflare REST because provider and "
    "served-model response headers are documented only for dynamic routes; "
    "metadata-only Logs API verification is required"
)

_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_VERCEL_ENDPOINT = "https://ai-gateway.vercel.sh/v1/chat/completions"
_CACHE_KEYS = frozenset({
    "cache",
    "cache_control",
    "cache_key",
    "prompt_cache_key",
})


class GatewayProfileError(ValueError):
    """Raised when a gateway profile cannot satisfy strict Gateway Tax."""


def validate_arm(
    *,
    route_kind: str,
    gateway: str | None,
    gateway_id: str | None,
    endpoint: str,
    requested_model: str,
    requested_provider: str,
    private_router: bool = False,
) -> None:
    """Validate profile-specific, nonsecret arm fields."""
    if route_kind == "direct":
        if gateway is not None:
            raise GatewayProfileError("direct arm must not declare gateway")
        if gateway_id is not None:
            raise GatewayProfileError("direct arm must not declare gateway_id")
        return
    if gateway is None:
        raise GatewayProfileError("gateway arm requires gateway")
    if gateway == "concentrate":
        raise GatewayProfileError(CONCENTRATE_UNSUPPORTED)
    if gateway == "cloudflare":
        raise GatewayProfileError(CLOUDFLARE_UNSUPPORTED)
    if gateway not in GATEWAYS:
        raise GatewayProfileError(
            f"gateway must be one of: {', '.join(sorted(GATEWAYS))}"
        )
    if gateway_id is not None:
        raise GatewayProfileError(f"{gateway} arm must not declare gateway_id")

    if private_router:
        return
    if gateway == "openrouter" and endpoint != _OPENROUTER_ENDPOINT:
        raise GatewayProfileError(
            f"openrouter endpoint must be {_OPENROUTER_ENDPOINT}"
        )
    if gateway == "vercel":
        if endpoint != _VERCEL_ENDPOINT:
            raise GatewayProfileError(f"vercel endpoint must be {_VERCEL_ENDPOINT}")
        if "/" not in requested_model:
            raise GatewayProfileError(
                "vercel requested_model must be a provider-qualified model ID"
            )
def _strip_cache_controls(value: Any) -> None:
    if isinstance(value, dict):
        for key in list(value):
            normalized = str(key).lower().replace("-", "_")
            if normalized in _CACHE_KEYS:
                value.pop(key)
            else:
                _strip_cache_controls(value[key])
    elif isinstance(value, list):
        for item in value:
            _strip_cache_controls(item)


def shape_body(
    payload: dict[str, Any],
    *,
    gateway: str,
    requested_provider: str,
) -> None:
    """Replace caller-controlled gateway routing and cache policy in-place."""
    _strip_cache_controls(payload)
    if gateway == "openrouter":
        payload["provider"] = {
            "only": [requested_provider],
            "allow_fallbacks": False,
        }
        payload.pop("providerOptions", None)
        return
    if gateway == "vercel":
        payload.pop("provider", None)
        for key in ("models", "order", "sort", "caching"):
            payload.pop(key, None)
        payload["providerOptions"] = {
            "gateway": {"only": [requested_provider]},
        }
        return
    raise GatewayProfileError(f"unsupported gateway profile: {gateway}")


def request_headers(
    *,
    gateway: str | None,
    gateway_id: str | None,
    secret: str,
) -> dict[str, str]:
    """Return authoritative auth and gateway control headers."""
    headers = {"Authorization": f"Bearer {secret}"}
    if gateway is None:
        return headers
    if gateway == "openrouter":
        headers.update({
            "X-OpenRouter-Metadata": "enabled",
            "X-OpenRouter-Cache": "false",
        })
    elif gateway != "vercel":
        raise GatewayProfileError(f"unsupported gateway profile: {gateway}")
    return headers


def blocked_request_header(name: str) -> bool:
    """Return whether an inbound header could alter managed gateway behavior."""
    normalized = name.lower()
    return (
        normalized == "x-openrouter-metadata"
        or normalized.startswith("x-openrouter-cache")
    )


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _clean_openrouter_attempts(value: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, list):
        return [], False
    attempts = []
    for item in value:
        if not isinstance(item, dict):
            return attempts, False
        attempt: dict[str, Any] = {}
        provider = _identifier(item.get("provider"))
        model = _identifier(item.get("model"))
        status = _integer(item.get("status"))
        if provider is not None:
            attempt["provider"] = provider
        if model is not None:
            attempt["model"] = model
        if status is not None:
            attempt["status"] = status
        if not attempt:
            return attempts, False
        attempts.append(attempt)
    return attempts, True


@dataclasses.dataclass(slots=True)
class GatewayEvidence:
    """Accumulate only privacy-safe routing metadata for one streamed response."""

    gateway: str
    requested_model: str
    requested_provider: str
    allowed_models: tuple[str, ...]
    allowed_providers: tuple[str, ...]
    response_headers: Mapping[str, str] = dataclasses.field(default_factory=dict)
    metadata_seen: bool = False
    metadata_requested_model: str | None = None
    served_model: str | None = None
    provider: str | None = None
    attempts: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    attempts_present: bool = False
    attempts_malformed: bool = False
    profile_reasons: list[str] = dataclasses.field(default_factory=list)
    safe_metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    def observe(self, obj: Mapping[str, Any]) -> bool:
        top_model = _identifier(obj.get("model"))
        top_provider = _identifier(obj.get("provider"))
        if self.gateway == "openrouter":
            if top_model is not None:
                self._set_model(top_model)
            if top_provider is not None:
                self._set_provider(top_provider)
            return self._observe_openrouter(obj)
        elif self.gateway == "vercel":
            return self._observe_vercel(obj, top_model, top_provider)
        return False

    def _set_provider(self, value: str) -> None:
        if self.provider is not None and self.provider.casefold() != value.casefold():
            self.profile_reasons.append("provider_conflict")
        self.provider = value

    def _set_model(self, value: str) -> None:
        if self.served_model is not None and self.served_model != value:
            self.profile_reasons.append("served_model_conflict")
        self.served_model = value

    def _observe_openrouter(self, obj: Mapping[str, Any]) -> bool:
        metadata = obj.get("openrouter_metadata")
        if not isinstance(metadata, dict):
            return False
        self.metadata_seen = True
        requested = _identifier(metadata.get("requested"))
        if requested is not None:
            self.metadata_requested_model = requested
        if "attempts" in metadata:
            raw_attempts = metadata.get("attempts")
            if isinstance(raw_attempts, list) and not raw_attempts:
                self.attempts = []
            else:
                self.attempts_present = True
                self.attempts, valid = _clean_openrouter_attempts(raw_attempts)
                self.attempts_malformed = not valid
        endpoints = metadata.get("endpoints")
        available = endpoints.get("available") if isinstance(endpoints, dict) else None
        if isinstance(available, list):
            for endpoint in available:
                if isinstance(endpoint, dict) and endpoint.get("selected") is True:
                    provider = _identifier(endpoint.get("provider"))
                    if provider is not None:
                        self._set_provider(provider)
                    break
        return True

    @staticmethod
    def _vercel_metadata(obj: Mapping[str, Any]) -> Mapping[str, Any] | None:
        containers = [obj]
        choices = obj.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, Mapping):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, Mapping):
                    containers.append(delta)
        for container in containers:
            for key in ("providerMetadata", "provider_metadata"):
                provider_metadata = container.get(key)
                if isinstance(provider_metadata, Mapping):
                    gateway = provider_metadata.get("gateway")
                    if isinstance(gateway, Mapping):
                        return gateway
        gateway = obj.get("gateway")
        return gateway if isinstance(gateway, Mapping) else None

    def _observe_vercel(
        self,
        obj: Mapping[str, Any],
        top_model: str | None,
        top_provider: str | None,
    ) -> bool:
        metadata = self._vercel_metadata(obj)
        if metadata is None:
            return False
        self.metadata_seen = True
        routing = metadata.get("routing")
        evidence = routing if isinstance(routing, Mapping) else metadata

        if "originalModelId" in evidence:
            self.metadata_requested_model = _identifier(
                evidence.get("originalModelId")
            )
        else:
            self.metadata_requested_model = self.requested_model

        final_provider = _identifier(evidence.get("finalProvider"))
        resolved_provider = _identifier(evidence.get("resolvedProvider"))
        if resolved_provider is not None:
            self._set_provider(resolved_provider)
        if final_provider is not None:
            self._set_provider(final_provider)
        elif resolved_provider is None and top_provider is not None:
            self._set_provider(top_provider)

        resolved_model = (
            _identifier(evidence.get("canonicalSlug"))
            or _identifier(evidence.get("resolvedProviderApiModelId"))
        )
        if resolved_model is not None:
            self._set_model(resolved_model)
        elif top_model is not None:
            self._set_model(top_model)
        if (
            top_model is not None
            and resolved_model is not None
            and top_model not in {self.requested_model, resolved_model}
        ):
            self.profile_reasons.append("served_model_conflict")

        counts = [
            _integer(evidence.get(key))
            for key in ("modelAttemptCount", "totalProviderAttemptCount")
            if key in evidence
        ]
        if not counts:
            self.profile_reasons.append("missing_attempt_count")
        elif any(count != 1 for count in counts):
            self.profile_reasons.append("multiple_attempts")

        model_attempts = evidence.get("modelAttempts")
        if isinstance(model_attempts, Mapping):
            model_attempts = [model_attempts]
        if not isinstance(model_attempts, list) or not model_attempts:
            self.profile_reasons.append("missing_model_attempts")
            return True
        if len(model_attempts) != 1:
            self.profile_reasons.append("multiple_attempts")
        provider_attempts = []
        self.attempts = []
        for model_attempt in model_attempts:
            if not isinstance(model_attempt, Mapping):
                self.attempts_malformed = True
                continue
            model_attempt_model = (
                _identifier(model_attempt.get("canonicalSlug"))
                or resolved_model
            )
            if (
                model_attempt_model is not None
                and resolved_model is not None
                and model_attempt_model != resolved_model
            ):
                self.profile_reasons.append("served_model_conflict")
            raw = model_attempt.get("providerAttempts")
            if not isinstance(raw, list):
                self.attempts_malformed = True
                continue
            provider_attempts.extend(
                (attempt, model_attempt_model, model_attempt.get("success"))
                for attempt in raw
            )
        self.attempts_present = True
        if len(provider_attempts) != 1:
            self.profile_reasons.append("multiple_attempts")
        successful_attempts = 0
        for raw, model_attempt_model, model_attempt_success in provider_attempts:
            if not isinstance(raw, Mapping):
                self.attempts_malformed = True
                continue
            provider = _identifier(raw.get("provider"))
            model = (
                _identifier(raw.get("resolvedProviderApiModelId"))
                or _identifier(raw.get("providerApiModelId"))
                or model_attempt_model
                or resolved_model
            )
            status = _integer(raw.get("statusCode"))
            provider_attempt_success = raw.get("success")
            if (
                provider_attempt_success is not None
                and not isinstance(provider_attempt_success, bool)
            ):
                self.attempts_malformed = True
            if (
                model_attempt_success is not None
                and not isinstance(model_attempt_success, bool)
            ):
                self.attempts_malformed = True
            if (
                isinstance(provider_attempt_success, bool)
                and isinstance(model_attempt_success, bool)
                and provider_attempt_success != model_attempt_success
            ):
                self.attempts_malformed = True
            if status is None and provider_attempt_success is True:
                status = 200
            status_success = status is not None and 200 <= status < 300
            if provider_attempt_success is True and not status_success:
                self.attempts_malformed = True
            if provider_attempt_success is False and status_success:
                self.attempts_malformed = True
            successful = (
                status_success
                and provider_attempt_success is not False
                and model_attempt_success is not False
            )
            if (
                model_attempt_success is False
                and provider_attempt_success is True
            ):
                self.attempts_malformed = True
            if successful:
                successful_attempts += 1
            attempt = {}
            if provider is not None:
                attempt["provider"] = provider
            if model is not None:
                attempt["model"] = model
            if status is not None:
                attempt["status"] = status
            self.attempts.append(attempt)
            if not provider or not model or status is None:
                self.attempts_malformed = True
        if len(self.attempts) != 1:
            self.profile_reasons.append("multiple_attempts")
        if successful_attempts != 1:
            self.profile_reasons.append("missing_successful_attempt")

        for key in ("generationId", "cost", "marketCost"):
            value = metadata.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                self.safe_metadata[key] = value
        return True

    def route_reasons(self) -> list[str]:
        reasons = list(self.profile_reasons)
        required = (
            (self.metadata_seen, f"missing_{self.gateway}_metadata"),
            (self.served_model, "missing_served_model"),
            (self.provider, "missing_provider"),
        )
        reasons.extend(reason for value, reason in required if not value)
        if (
            self.served_model
            and self.allowed_models
            and self.served_model not in self.allowed_models
        ):
            reasons.append("served_model_not_allowed")
        if (
            self.provider
            and self.requested_provider
            and self.provider.casefold()
            != self.requested_provider.casefold()
        ):
            reasons.append("provider_conflict")
        allowed = {provider.casefold() for provider in self.allowed_providers}
        if self.provider and allowed and self.provider.casefold() not in allowed:
            reasons.append("provider_not_allowed")
        if self.gateway in {"openrouter", "vercel"}:
            if not self.metadata_requested_model:
                reasons.append("missing_metadata_requested_model")
            elif self.metadata_requested_model != self.requested_model:
                reasons.append("requested_model_conflict")
        if self.gateway == "vercel" and not self.attempts_present:
            reasons.append("missing_attempt_evidence")
        if self.attempts_malformed:
            reasons.append("malformed_attempts")
        if (
            self.attempts_present
            and self.attempts
            and not any(
                isinstance(attempt.get("status"), int)
                and 200 <= attempt["status"] < 300
                for attempt in self.attempts
            )
        ):
            reasons.append("missing_successful_attempt")
        for attempt in self.attempts:
            provider = _identifier(attempt.get("provider"))
            model = _identifier(attempt.get("model"))
            status = _integer(attempt.get("status"))
            if provider is None:
                reasons.append("missing_attempt_provider")
            elif (
                self.provider
                and provider.casefold() != self.provider.casefold()
            ):
                reasons.append("fallback_attempt")
            if model is None:
                reasons.append("missing_attempt_model")
            elif (
                self.allowed_models
                and model not in self.allowed_models
            ):
                reasons.append("fallback_attempt")
            if status is None:
                reasons.append("missing_attempt_status")
            elif not 200 <= status < 300:
                reasons.append("unsuccessful_attempt")
        return list(dict.fromkeys(reasons))
