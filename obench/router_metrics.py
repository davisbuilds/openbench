#!/usr/bin/env python3
"""Privacy-preserving metrics and route evidence for OpenAI streaming APIs.

The parser accepts response chunks with caller-supplied monotonic timestamps.
It deliberately exposes no event payloads or generated text: snapshots contain
only timing, token accounting, model/provider identifiers, and coverage state.
"""

from __future__ import annotations

import codecs
import json
import math
from collections.abc import Mapping
from typing import Any, Iterable

from . import router_gateways


_SEMANTIC_DELTA_KEYS = ("content", "reasoning_content", "reasoning", "refusal")


def _timestamp(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _has_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, list):
        return any(_has_text(item) for item in value)
    if isinstance(value, dict):
        return any(
            _has_text(value.get(key))
            for key in ("text", "content", "arguments", "name", "function")
        )
    return False


def _has_semantic_delta(obj: dict[str, Any]) -> bool:
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if any(_has_text(delta.get(key)) for key in _SEMANTIC_DELTA_KEYS):
            return True
        if _has_text(delta.get("tool_calls")) or _has_text(delta.get("function_call")):
            return True
    return False


def _has_responses_semantic_delta(obj: dict[str, Any]) -> bool:
    event_type = obj.get("type")
    if event_type in {
        "response.output_text.delta",
        "response.refusal.delta",
        "response.function_call_arguments.delta",
    }:
        return _has_text(obj.get("delta"))
    return False


class OpenAIChatSSEParser:
    """Incrementally derive metrics from an OpenAI Chat Completions SSE body."""

    def __init__(
        self,
        *,
        requested_model: str | None,
        started_at: float,
        route_kind: str = "gateway",
        requested_provider: str | None = None,
        allowed_models: Iterable[str] = (),
        allowed_providers: Iterable[str] = (),
        fallback_enabled: bool = False,
        router_mode: str | None = None,
        model_match: str = "exact_revision",
        gateway: str | None = None,
        response_headers: Mapping[str, str] | None = None,
    ):
        self._started_at = _timestamp(started_at, "started_at")
        self._requested_model = _identifier(requested_model)
        if route_kind not in {"direct", "gateway"}:
            raise ValueError("route_kind must be direct or gateway")
        self._route_kind = route_kind
        self._requested_provider = _identifier(requested_provider)
        self._allowed_models = tuple(
            model for value in allowed_models if (model := _identifier(value)) is not None
        )
        self._allowed_providers = tuple(
            provider
            for value in allowed_providers
            if (provider := _identifier(value)) is not None
        )
        self._fallback_enabled = bool(fallback_enabled)
        self._router_mode = router_mode
        self._model_match = model_match
        self._gateway = gateway or ("openrouter" if route_kind == "gateway" else None)
        self._gateway_evidence = (
            router_gateways.GatewayEvidence(
                gateway=self._gateway,
                requested_model=self._requested_model or "",
                requested_provider=self._requested_provider or "",
                allowed_models=self._allowed_models,
                allowed_providers=self._allowed_providers,
                router_mode=self._router_mode,
                model_match=model_match,
                response_headers=response_headers or {},
            )
            if self._gateway is not None
            else None
        )
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._chunk_type: type | None = None
        self._line = ""
        self._line_observed_at: float | None = None
        self._pending_cr = False
        self._data_lines: list[str] = []
        self._event_data_at: float | None = None
        self._first_byte_at: float | None = None
        self._first_semantic_at: float | None = None
        self._last_received_at: float | None = None
        self._completed_at: float | None = None
        self._served_model: str | None = None
        self._direct_dated_model_ids: set[str] = set()
        self._direct_model_conflict = False
        self._direct_provider_conflict = False
        self._top_level_provider: str | None = None
        self._usage: dict[str, Any] | None = None
        self._events = 0
        self._ignored_events = 0
        self._malformed_events = 0
        self._done_seen = False
        self._finalized = False

    def feed(self, chunk: bytes | str, received_at: float) -> None:
        """Consume one arbitrary response fragment observed at ``received_at``."""
        if self._finalized:
            raise RuntimeError("cannot feed a finalized parser")
        received_at = _timestamp(received_at, "received_at")
        if received_at < self._started_at:
            raise ValueError("received_at precedes started_at")
        if self._last_received_at is not None and received_at < self._last_received_at:
            raise ValueError("received_at must be monotonic")
        if not isinstance(chunk, (bytes, str)):
            raise TypeError("chunk must be bytes or str")
        chunk_type = type(chunk)
        if self._chunk_type is not None and chunk_type is not self._chunk_type:
            raise TypeError("cannot mix bytes and str chunks")
        self._chunk_type = chunk_type
        if chunk:
            if self._first_byte_at is None:
                self._first_byte_at = received_at
        self._last_received_at = received_at
        text = self._decoder.decode(chunk, final=False) if isinstance(chunk, bytes) else chunk
        self._consume_text(text, received_at)

    def finalize(self, completed_at: float | None = None) -> dict[str, Any]:
        """Close the stream and return a privacy-safe metrics snapshot."""
        if not self._finalized:
            if completed_at is None:
                completed_at = self._last_received_at
            if completed_at is not None:
                completed_at = _timestamp(completed_at, "completed_at")
                if completed_at < self._started_at:
                    raise ValueError("completed_at precedes started_at")
                if self._last_received_at is not None and completed_at < self._last_received_at:
                    raise ValueError("completed_at precedes the last chunk")
            tail = self._decoder.decode(b"", final=True)
            terminal_at = completed_at
            if terminal_at is None:
                terminal_at = self._last_received_at
            if terminal_at is None:
                terminal_at = self._started_at
            if tail:
                self._consume_text(tail, terminal_at)
            if self._pending_cr:
                self._emit_line(terminal_at)
                self._pending_cr = False
            if self._line or self._data_lines:
                self._malformed_events += 1
            self._line = ""
            self._line_observed_at = None
            self._data_lines.clear()
            self._event_data_at = None
            self._decoder = None
            self._completed_at = completed_at
            self._finalized = True
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """Return current derived state without retaining or exposing SSE content."""
        ttfb = self._elapsed(self._first_byte_at, self._started_at)
        ttft = self._elapsed(self._first_semantic_at, self._started_at)
        generation_duration = self._elapsed(self._completed_at, self._first_semantic_at)
        output_tokens = (self._usage or {}).get("output_tokens")
        generation = None
        if output_tokens is not None and generation_duration is not None:
            generation = {
                "output_tokens": output_tokens,
                "duration_s": generation_duration,
                "tokens_per_second": (
                    output_tokens / generation_duration if generation_duration > 0 else None
                ),
            }

        gateway_evidence = self._gateway_evidence
        provider = (
            gateway_evidence.provider
            if gateway_evidence is not None
            else self._resolved_provider()
        )
        served_model = (
            gateway_evidence.served_model
            if gateway_evidence is not None
            else self._served_model
        )
        metadata_requested_model = (
            gateway_evidence.metadata_requested_model
            if gateway_evidence is not None
            else None
        )
        attempts = (
            gateway_evidence.attempts
            if gateway_evidence is not None
            else []
        )
        coverage = {
            "ttfb": ttfb is not None,
            "semantic_ttft": ttft is not None,
            "usage": self._usage is not None,
            "generation": generation is not None,
            "requested_model": self._requested_model is not None,
            "served_model": served_model is not None,
            "openrouter_metadata": (
                gateway_evidence.metadata_seen
                if gateway_evidence is not None
                else False
            ),
            "provider": provider is not None,
            "attempts": bool(attempts),
            "attempt_evidence": (
                gateway_evidence.attempts_present
                if gateway_evidence is not None
                else False
            ),
            "stream_done": self._done_seen,
        }
        covered = sum(coverage.values())
        route = {
            "requested_model": self._requested_model,
            "metadata_requested_model": metadata_requested_model,
            "served_model": served_model,
            "provider": provider,
            "attempts": [dict(attempt) for attempt in attempts],
        }
        if gateway_evidence is not None and gateway_evidence.safe_metadata:
            route["gateway_metadata"] = dict(gateway_evidence.safe_metadata)
        return {
            "timing": {
                "ttfb_s": ttfb,
                "semantic_ttft_s": ttft,
                "total_s": self._elapsed(self._completed_at, self._started_at),
            },
            "usage": dict(self._usage) if self._usage is not None else None,
            "generation": generation,
            "route": route,
            "coverage": {
                **coverage,
                "covered": covered,
                "total": len(coverage),
                "ratio": covered / len(coverage),
            },
            "route_evidence": self._route_evidence(provider, served_model),
            "stream": {
                "events": self._events,
                "ignored_events": self._ignored_events,
                "malformed_events": self._malformed_events,
                "done": self._done_seen,
                "finalized": self._finalized,
            },
        }

    @staticmethod
    def _elapsed(end: float | None, start: float | None) -> float | None:
        if end is None or start is None:
            return None
        return max(0.0, end - start)

    def _consume_text(self, text: str, timestamp: float) -> None:
        for char in text:
            if self._pending_cr:
                self._emit_line(timestamp)
                self._pending_cr = False
                if char == "\n":
                    continue
            if char == "\r":
                self._pending_cr = True
            elif char == "\n":
                self._emit_line(timestamp)
            else:
                self._line += char
                self._line_observed_at = timestamp

    def _emit_line(self, timestamp: float) -> None:
        line, self._line = self._line, ""
        line_observed_at, self._line_observed_at = self._line_observed_at, None
        if not line:
            self._dispatch(timestamp)
            return
        if line.startswith(":"):
            return
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data_lines.append(value)
            self._event_data_at = line_observed_at if line_observed_at is not None else timestamp

    def _dispatch(self, timestamp: float) -> None:
        if not self._data_lines:
            return
        data = "\n".join(self._data_lines)
        event_data_at = self._event_data_at if self._event_data_at is not None else timestamp
        self._data_lines.clear()
        self._event_data_at = None
        if not data.strip():
            self._ignored_events += 1
            return
        if data.strip() == "[DONE]":
            if self._accept_done_marker():
                self._done_seen = True
            self._events += 1
            return
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, UnicodeError):
            self._malformed_events += 1
            return
        if not isinstance(obj, dict):
            self._ignored_events += 1
            return
        if self._is_terminal_event(obj):
            self._done_seen = True
        if self._observe(obj, event_data_at):
            self._events += 1
        else:
            self._ignored_events += 1

    def _metric_objects(self, obj: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        return (obj,)

    def _accept_done_marker(self) -> bool:
        return True

    def _is_terminal_event(self, obj: dict[str, Any]) -> bool:
        return False

    def _has_semantic_content(self, obj: dict[str, Any]) -> bool:
        return _has_semantic_delta(obj)

    def _observe(self, obj: dict[str, Any], timestamp: float) -> bool:
        gateway_metadata_observed = False
        usage_observed = False
        for metric_obj in self._metric_objects(obj):
            if self._gateway_evidence is not None:
                gateway_metadata_observed = (
                    self._gateway_evidence.observe(metric_obj)
                    or gateway_metadata_observed
                )
            model = _identifier(metric_obj.get("model"))
            if model is not None:
                if self._route_kind == "direct" and self._model_match == "rolling_alias":
                    revision = router_gateways.concrete_model_revision(model)
                    if revision is not None:
                        self._direct_dated_model_ids.add(revision)
                        if len(self._direct_dated_model_ids) > 1:
                            self._direct_model_conflict = True
                    if (
                        self._requested_provider
                        and not router_gateways.model_provider_matches(
                            model, self._requested_provider
                        )
                    ):
                        self._direct_model_conflict = True
                self._served_model = model
            provider = _identifier(metric_obj.get("provider"))
            if provider is not None:
                if (
                    self._route_kind == "direct"
                    and self._requested_provider
                    and provider.casefold() != self._requested_provider.casefold()
                ):
                    self._direct_provider_conflict = True
                self._top_level_provider = provider

            usage = metric_obj.get("usage")
            if isinstance(usage, dict):
                prompt = _token_count(usage.get("prompt_tokens"))
                completion = _token_count(usage.get("completion_tokens"))
                if prompt is None:
                    prompt = _token_count(usage.get("input_tokens"))
                if completion is None:
                    completion = _token_count(usage.get("output_tokens"))
                total = _token_count(usage.get("total_tokens"))
                clean = {}
                if prompt is not None:
                    clean["input_tokens"] = prompt
                if completion is not None:
                    clean["output_tokens"] = completion
                if total is not None:
                    clean["total_tokens"] = total
                raw_details = usage.get("input_tokens_details")
                if raw_details is None:
                    raw_details = usage.get("prompt_tokens_details")
                if isinstance(raw_details, Mapping):
                    details = {}
                    for key in (
                        "cached_tokens",
                        "cache_write_tokens",
                        "cached_tokens_created",
                    ):
                        count = _token_count(raw_details.get(key))
                        if count is not None:
                            details[key] = count
                    if details:
                        clean["input_tokens_details"] = details
                if clean:
                    self._usage = clean
                    usage_observed = True

        semantic = self._has_semantic_content(obj)
        if self._first_semantic_at is None and semantic:
            self._first_semantic_at = timestamp
        return (
            semantic
            or gateway_metadata_observed
            or usage_observed
            or self._is_terminal_event(obj)
        )

    def _resolved_provider(self) -> str | None:
        return self._top_level_provider

    def _route_evidence(
        self,
        provider: str | None,
        served_model: str | None,
    ) -> dict[str, Any]:
        reasons = []
        required = (
            (self._requested_model, "missing_requested_model"),
            (served_model, "missing_served_model"),
            (self._done_seen, "stream_not_done"),
            (self._malformed_events == 0, "malformed_events"),
        )
        reasons.extend(reason for value, reason in required if not value)

        if self._route_kind == "gateway":
            if self._gateway_evidence is None:
                reasons.append("missing_gateway_profile")
            else:
                reasons.extend(self._gateway_evidence.route_reasons())
            if self._fallback_enabled and self._router_mode != "auto":
                reasons.append("fallback_enabled")
        else:
            if self._direct_model_conflict:
                reasons.append("served_model_conflict")
            if (
                self._requested_model
                and isinstance(served_model, str)
                and (
                    not router_gateways.models_match(
                        self._requested_model, served_model, self._model_match
                    )
                    or (
                        self._requested_provider
                        and not router_gateways.model_provider_matches(
                            served_model, self._requested_provider
                        )
                    )
                )
            ):
                reasons.append("served_model_conflict")
            if (
                self._direct_provider_conflict
                or (
                    provider is not None
                    and self._requested_provider
                    and provider.casefold() != self._requested_provider.casefold()
                )
            ):
                reasons.append("provider_conflict")
        return {
            "pass": not reasons,
            "verdict": "pass" if not reasons else "fail",
            "reasons": list(dict.fromkeys(reasons)),
        }


# Short alias for callers that already know the protocol context.
ChatSSEMetricsParser = OpenAIChatSSEParser


class OpenAIResponsesSSEParser(OpenAIChatSSEParser):
    """Incrementally derive metrics from an OpenAI Responses SSE body."""

    def _metric_objects(self, obj: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        response = obj.get("response")
        if isinstance(response, dict):
            return obj, response
        return (obj,)

    def _accept_done_marker(self) -> bool:
        return False

    def _is_terminal_event(self, obj: dict[str, Any]) -> bool:
        return obj.get("type") in {
            "response.completed",
            "response.incomplete",
            "response.failed",
        }

    def _has_semantic_content(self, obj: dict[str, Any]) -> bool:
        return _has_responses_semantic_delta(obj)


def sse_parser(protocol: str, **kwargs: Any) -> OpenAIChatSSEParser:
    """Construct the strict parser for a Router Bench wire protocol."""
    if protocol == "openai_chat":
        return OpenAIChatSSEParser(**kwargs)
    if protocol == "openai_responses":
        return OpenAIResponsesSSEParser(**kwargs)
    raise ValueError(f"unsupported router protocol: {protocol}")


def parse_chat_sse(
    chunks: Iterable[tuple[float, bytes | str]],
    *,
    requested_model: str | None,
    started_at: float,
    completed_at: float | None = None,
    route_kind: str = "gateway",
    requested_provider: str | None = None,
    allowed_models: Iterable[str] = (),
    allowed_providers: Iterable[str] = (),
    fallback_enabled: bool = False,
    router_mode: str | None = None,
    model_match: str = "exact_revision",
    gateway: str | None = None,
    response_headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Replay timestamped chunks through :class:`OpenAIChatSSEParser`."""
    parser = OpenAIChatSSEParser(
        requested_model=requested_model,
        started_at=started_at,
        route_kind=route_kind,
        requested_provider=requested_provider,
        allowed_models=allowed_models,
        allowed_providers=allowed_providers,
        fallback_enabled=fallback_enabled,
        router_mode=router_mode,
        model_match=model_match,
        gateway=gateway,
        response_headers=response_headers,
    )
    for received_at, chunk in chunks:
        parser.feed(chunk, received_at)
    return parser.finalize(completed_at)


def parse_responses_sse(
    chunks: Iterable[tuple[float, bytes | str]],
    *,
    requested_model: str | None,
    started_at: float,
    completed_at: float | None = None,
    route_kind: str = "gateway",
    requested_provider: str | None = None,
    allowed_models: Iterable[str] = (),
    allowed_providers: Iterable[str] = (),
    fallback_enabled: bool = False,
    router_mode: str | None = None,
    model_match: str = "exact_revision",
    gateway: str | None = None,
    response_headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Replay timestamped chunks through :class:`OpenAIResponsesSSEParser`."""
    parser = OpenAIResponsesSSEParser(
        requested_model=requested_model,
        started_at=started_at,
        route_kind=route_kind,
        requested_provider=requested_provider,
        allowed_models=allowed_models,
        allowed_providers=allowed_providers,
        fallback_enabled=fallback_enabled,
        router_mode=router_mode,
        model_match=model_match,
        gateway=gateway,
        response_headers=response_headers,
    )
    for received_at, chunk in chunks:
        parser.feed(chunk, received_at)
    return parser.finalize(completed_at)
