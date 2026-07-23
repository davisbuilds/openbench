#!/usr/bin/env python3
"""Privacy-preserving metrics and route evidence for OpenAI Chat SSE streams.

The parser accepts response chunks with caller-supplied monotonic timestamps.
It deliberately exposes no event payloads or generated text: snapshots contain
only timing, token accounting, model/provider identifiers, and coverage state.
"""

from __future__ import annotations

import codecs
import json
import math
from typing import Any, Iterable


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


def _clean_attempts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    attempts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        attempt = {}
        provider = _identifier(item.get("provider"))
        model = _identifier(item.get("model"))
        status = item.get("status")
        if provider is not None:
            attempt["provider"] = provider
        if model is not None:
            attempt["model"] = model
        if isinstance(status, int) and not isinstance(status, bool):
            attempt["status"] = status
        if attempt:
            attempts.append(attempt)
    return attempts


def _successful_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for attempt in reversed(attempts):
        status = attempt.get("status")
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            continue
        if 200 <= status_code < 300:
            return attempt
    return None


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
        self._top_level_provider: str | None = None
        self._metadata_requested: str | None = None
        self._metadata_seen = False
        self._metadata_provider: str | None = None
        self._attempts: list[dict[str, Any]] = []
        self._attempts_present = False
        self._usage: dict[str, int] | None = None
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

        provider = self._resolved_provider()
        coverage = {
            "ttfb": ttfb is not None,
            "semantic_ttft": ttft is not None,
            "usage": self._usage is not None,
            "generation": generation is not None,
            "requested_model": self._requested_model is not None,
            "served_model": self._served_model is not None,
            "openrouter_metadata": self._metadata_seen,
            "provider": provider is not None,
            "attempts": bool(self._attempts),
            "stream_done": self._done_seen,
        }
        covered = sum(coverage.values())
        return {
            "timing": {
                "ttfb_s": ttfb,
                "semantic_ttft_s": ttft,
                "total_s": self._elapsed(self._completed_at, self._started_at),
            },
            "usage": dict(self._usage) if self._usage is not None else None,
            "generation": generation,
            "route": {
                "requested_model": self._requested_model,
                "metadata_requested_model": self._metadata_requested,
                "served_model": self._served_model,
                "provider": provider,
                "attempts": [dict(attempt) for attempt in self._attempts],
            },
            "coverage": {
                **coverage,
                "covered": covered,
                "total": len(coverage),
                "ratio": covered / len(coverage),
            },
            "route_evidence": self._route_evidence(provider),
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
        if self._observe(obj, event_data_at):
            self._events += 1
        else:
            self._ignored_events += 1

    def _observe(self, obj: dict[str, Any], timestamp: float) -> bool:
        model = _identifier(obj.get("model"))
        if model is not None:
            self._served_model = model
        provider = _identifier(obj.get("provider"))
        if provider is not None:
            self._top_level_provider = provider

        metadata = obj.get("openrouter_metadata")
        if isinstance(metadata, dict):
            self._metadata_seen = True
            requested = _identifier(metadata.get("requested"))
            if requested is not None:
                self._metadata_requested = requested
            raw_attempts = metadata.get("attempts")
            if isinstance(raw_attempts, list) and raw_attempts:
                self._attempts_present = True
                self._attempts = _clean_attempts(raw_attempts)
            endpoints = metadata.get("endpoints")
            available = endpoints.get("available") if isinstance(endpoints, dict) else None
            if isinstance(available, list):
                for endpoint in available:
                    if isinstance(endpoint, dict) and endpoint.get("selected") is True:
                        self._metadata_provider = _identifier(endpoint.get("provider"))
                        break

        usage = obj.get("usage")
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
            if clean:
                self._usage = clean

        semantic = _has_semantic_delta(obj)
        if self._first_semantic_at is None and semantic:
            self._first_semantic_at = timestamp
        return (
            semantic
            or isinstance(metadata, dict)
            or (self._usage is not None and isinstance(usage, dict))
        )

    def _resolved_provider(self) -> str | None:
        return self._top_level_provider or self._metadata_provider

    def _route_evidence(self, provider: str | None) -> dict[str, Any]:
        reasons = []
        required = (
            (self._requested_model, "missing_requested_model"),
            (self._served_model, "missing_served_model"),
            (self._done_seen, "stream_not_done"),
            (self._malformed_events == 0, "malformed_events"),
        )
        reasons.extend(reason for value, reason in required if not value)
        if self._requested_model and self._served_model != self._requested_model:
            reasons.append("served_model_conflict")

        if self._route_kind == "gateway":
            gateway_required = (
                (self._metadata_seen, "missing_openrouter_metadata"),
                (self._metadata_requested, "missing_metadata_requested_model"),
                (provider, "missing_provider"),
            )
            reasons.extend(reason for value, reason in gateway_required if not value)
            if self._metadata_requested and self._metadata_requested != self._requested_model:
                reasons.append("requested_model_conflict")
            if self._allowed_models and self._served_model not in self._allowed_models:
                reasons.append("served_model_not_allowed")

            expected_provider = self._requested_provider
            provider_folded = provider.casefold() if provider else None
            if expected_provider and provider_folded != expected_provider.casefold():
                reasons.append("provider_conflict")
            allowed_provider_folds = {
                allowed.casefold() for allowed in self._allowed_providers
            }
            if provider and allowed_provider_folds and provider_folded not in allowed_provider_folds:
                reasons.append("provider_not_allowed")
            if (
                self._top_level_provider
                and self._metadata_provider
                and self._top_level_provider.casefold() != self._metadata_provider.casefold()
            ):
                reasons.append("provider_conflict")

            if self._attempts_present:
                if not self._attempts:
                    reasons.append("malformed_attempts")
                success = _successful_attempt(self._attempts)
                if success is None:
                    reasons.append("missing_successful_attempt")
                for attempt in self._attempts:
                    attempt_provider = _identifier(attempt.get("provider"))
                    attempt_model = _identifier(attempt.get("model"))
                    if not attempt_provider:
                        reasons.append("missing_attempt_provider")
                    elif provider_folded and attempt_provider.casefold() != provider_folded:
                        reasons.append("fallback_attempt")
                    if not attempt_model:
                        reasons.append("missing_attempt_model")
                    elif self._served_model and attempt_model != self._served_model:
                        reasons.append("fallback_attempt")
                if self._fallback_enabled:
                    reasons.append("fallback_enabled")
        return {
            "pass": not reasons,
            "verdict": "pass" if not reasons else "fail",
            "reasons": list(dict.fromkeys(reasons)),
        }


# Short alias for callers that already know the protocol context.
ChatSSEMetricsParser = OpenAIChatSSEParser


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
    )
    for received_at, chunk in chunks:
        parser.feed(chunk, received_at)
    return parser.finalize(completed_at)
