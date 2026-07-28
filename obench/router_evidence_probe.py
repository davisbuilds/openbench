"""Live, privacy-safe evidence probe for native model routers.

This module does not score router quality. It proves which routing facts can be
captured, reconciled, and safely published before Router Bench depends on them.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Mapping
from typing import Any


SCHEMA_VERSION = 1
SUPPORTED_ROUTERS = frozenset({"concentrate", "openrouter"})
PROBE_CASES = {
    "exact": "Return exactly the word READY.",
    "debug": (
        "A Python function uses `def append(value, items=[])`. Briefly explain "
        "the bug and provide the corrected function signature."
    ),
    "architecture": (
        "Give a concise design for an idempotent background-job worker that "
        "can safely retry after a crash."
    ),
}
_ROUTER_CONFIG = {
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1/responses",
        "requested_router": "openrouter/auto-beta",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "concentrate": {
        "endpoint": "https://api.concentrate.ai/v1/responses",
        "requested_router": "auto",
        "api_key_env": "CONCENTRATE_API_KEY",
    },
}
_FORBIDDEN_ARTIFACT_KEYS = frozenset({
    "authorization", "api_key", "content", "input", "output", "prompt_text",
})


class RouterEvidenceError(RuntimeError):
    """Raised when evidence cannot be safely captured or verified."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _finite_nonnegative(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or not math.isfinite(float(value)):
        return None
    return value


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _provider_from_model(model: str | None) -> str | None:
    if not model or "/" not in model:
        return None
    return model.split("/", 1)[0]


def _usage_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key in ("input_tokens", "prompt_tokens", "output_tokens",
                "completion_tokens", "total_tokens", "cost"):
        number = _finite_nonnegative(value.get(key))
        if number is not None:
            result[key] = number
    input_details = value.get("input_tokens_details")
    if not isinstance(input_details, Mapping):
        input_details = value.get("prompt_tokens_details")
    if isinstance(input_details, Mapping):
        cached = _finite_nonnegative(input_details.get("cached_tokens"))
        if cached is not None:
            result["cached_input_tokens"] = cached
    output_details = value.get("output_tokens_details")
    if not isinstance(output_details, Mapping):
        output_details = value.get("completion_tokens_details")
    if isinstance(output_details, Mapping):
        reasoning = _finite_nonnegative(output_details.get("reasoning_tokens"))
        if reasoning is not None:
            result["reasoning_tokens"] = reasoning
    cost_details = value.get("cost_details")
    if isinstance(cost_details, Mapping):
        upstream = _finite_nonnegative(cost_details.get("upstream_inference_cost"))
        if upstream is not None:
            result["upstream_inference_cost"] = upstream
    return result


def _usage_checks(usage: Mapping[str, Any]) -> dict[str, bool]:
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens")
    checks = {
        "usage_present": bool(usage),
        "token_counts_present": all(
            isinstance(value, (int, float))
            for value in (input_tokens, output_tokens, total_tokens)
        ),
    }
    if checks["token_counts_present"]:
        checks["token_total_consistent"] = (
            input_tokens + output_tokens == total_tokens
        )
    return checks


def _dispatch_sse_block(
    data_lines: list[str],
    *,
    events: list[str],
    state: dict[str, Any],
) -> None:
    if not data_lines:
        return
    raw = "\n".join(data_lines).strip()
    data_lines.clear()
    if not raw or raw == "[DONE]":
        return
    try:
        item = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RouterEvidenceError("invalid JSON in SSE data event") from exc
    if not isinstance(item, dict):
        raise RouterEvidenceError("SSE data event must be a JSON object")
    event_type = _identifier(item.get("type")) or "unknown"
    events.append(event_type)
    response = item.get("response")
    if isinstance(response, dict):
        state["response"] = response
    elif event_type in {"response.completed", "response.incomplete",
                       "response.failed"}:
        state["response"] = item
    if event_type == "error" or "error" in item:
        state["error"] = item.get("error") or item


def parse_sse_lines(lines: Iterable[bytes | str]) -> tuple[dict[str, Any], list[str]]:
    """Return the terminal Responses object and event-type sequence."""
    data_lines: list[str] = []
    events: list[str] = []
    state: dict[str, Any] = {}
    for raw_line in lines:
        line = (
            raw_line.decode("utf-8", errors="strict")
            if isinstance(raw_line, bytes)
            else raw_line
        ).rstrip("\r\n")
        if not line:
            _dispatch_sse_block(data_lines, events=events, state=state)
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    _dispatch_sse_block(data_lines, events=events, state=state)
    if state.get("error") is not None:
        raise RouterEvidenceError(
            f"stream returned an error: {_canonical_json(state['error'])}"
        )
    response = state.get("response")
    if not isinstance(response, dict):
        raise RouterEvidenceError("stream did not contain a terminal response")
    return response, events


def _request_stream(
    *,
    endpoint: str,
    api_key: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float = 120.0,
) -> tuple[dict[str, Any], dict[str, str], dict[str, float], list[str]]:
    request = urllib.request.Request(
        endpoint,
        data=_canonical_json(dict(payload)).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "OpenBench/1.0 router-evidence-probe",
            **dict(headers),
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_headers = {
                key.casefold(): value for key, value in response.headers.items()
            }
            first_event_at = None

            def measured_lines():
                nonlocal first_event_at
                for line in response:
                    if first_event_at is None and line.startswith(b"data:"):
                        first_event_at = time.monotonic()
                    yield line

            terminal, events = parse_sse_lines(measured_lines())
            finished = time.monotonic()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"status": exc.code, "body_prefix": body[:400]}
        raise RouterEvidenceError(
            f"router request failed: {_canonical_json(detail)}"
        ) from exc
    timings = {
        "first_event_s": (
            (first_event_at - started) if first_event_at is not None else None
        ),
        "stream_total_s": finished - started,
    }
    return terminal, response_headers, timings, events


def _request_json(
    url: str,
    *,
    api_key: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "OpenBench/1.0 router-evidence-probe",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise
        body = exc.read().decode("utf-8", errors="replace")
        raise RouterEvidenceError(
            f"trace lookup failed with HTTP {exc.code}: {body[:400]}"
        ) from exc
    if not isinstance(value, dict):
        raise RouterEvidenceError("trace lookup did not return a JSON object")
    return value


def _poll_openrouter_trace(
    generation_id: str,
    *,
    api_key: str,
    timeout: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + max(timeout, 0.0)
    delay = 0.0
    url = (
        "https://openrouter.ai/api/v1/generation?id="
        + urllib.parse.quote(generation_id, safe="")
    )
    while True:
        if delay:
            time.sleep(delay)
        try:
            value = _request_json(url, api_key=api_key)
            data = value.get("data", value)
            return data if isinstance(data, dict) else None
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        delay = min(0.5 if delay == 0 else delay * 2, 4.0, remaining)


def _openrouter_route(response: Mapping[str, Any]) -> dict[str, Any]:
    metadata = response.get("openrouter_metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    endpoints = metadata.get("endpoints")
    available = (
        endpoints.get("available", [])
        if isinstance(endpoints, Mapping)
        else []
    )
    selected = []
    candidate_models = set()
    for endpoint in available if isinstance(available, list) else []:
        if not isinstance(endpoint, Mapping):
            continue
        model = _identifier(endpoint.get("model"))
        provider = _identifier(endpoint.get("provider"))
        if model:
            candidate_models.add(model)
        if endpoint.get("selected") is True:
            selected.append({"model": model, "provider": provider})
    attempts = metadata.get("attempts")
    clean_attempts = []
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, Mapping):
                clean_attempts.append({
                    "model": _identifier(attempt.get("model")),
                    "provider": _identifier(attempt.get("provider")),
                    "status": attempt.get("status")
                    if isinstance(attempt.get("status"), int) else None,
                })
    pipeline = []
    for stage in metadata.get("pipeline", []):
        if not isinstance(stage, Mapping):
            continue
        data = stage.get("data")
        pipeline.append({
            "type": _identifier(stage.get("type")),
            "name": _identifier(stage.get("name")),
            "resolved_to": (
                _identifier(data.get("resolved_to"))
                if isinstance(data, Mapping) else None
            ),
            "fallback_models": (
                [
                    model for model in data.get("fallback_models", [])
                    if isinstance(model, str)
                ]
                if isinstance(data, Mapping)
                and isinstance(data.get("fallback_models"), list)
                else []
            ),
        })
    return {
        "requested": _identifier(metadata.get("requested")),
        "strategy": _identifier(metadata.get("strategy")),
        "attempt": metadata.get("attempt")
        if isinstance(metadata.get("attempt"), int) else None,
        "candidate_count": (
            endpoints.get("total")
            if isinstance(endpoints, Mapping)
            and isinstance(endpoints.get("total"), int)
            else len(candidate_models)
        ),
        "candidate_models": sorted(candidate_models),
        "selected_endpoints": selected,
        "attempts": clean_attempts,
        "pipeline": pipeline,
    }


def _clean_openrouter_trace(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    provider_responses = []
    raw_responses = value.get("provider_responses")
    if isinstance(raw_responses, list):
        for item in raw_responses:
            if not isinstance(item, Mapping):
                continue
            provider_responses.append({
                "model": _identifier(item.get("model_permaslug")),
                "provider": _identifier(item.get("provider_name")),
                "status": item.get("status")
                if isinstance(item.get("status"), int) else None,
                "upstream_id_hash": (
                    hashlib.sha256(item["id"].encode("utf-8")).hexdigest()
                    if isinstance(item.get("id"), str) else None
                ),
            })
    return {
        "id": _identifier(value.get("id")),
        "request_id": _identifier(value.get("request_id")),
        "model": _identifier(value.get("model")),
        "provider": _identifier(value.get("provider_name")),
        "router": _identifier(value.get("router")),
        "total_cost": _finite_nonnegative(value.get("total_cost")),
        "tokens_prompt": _finite_nonnegative(value.get("tokens_prompt")),
        "tokens_completion": _finite_nonnegative(value.get("tokens_completion")),
        "native_tokens_prompt": _finite_nonnegative(
            value.get("native_tokens_prompt")
        ),
        "native_tokens_completion": _finite_nonnegative(
            value.get("native_tokens_completion")
        ),
        "provider_responses": provider_responses,
    }


def reconcile_openrouter(
    *,
    requested_router: str,
    response: Mapping[str, Any],
    response_headers: Mapping[str, str],
    trace: Mapping[str, Any] | None,
) -> dict[str, Any]:
    route = _openrouter_route(response)
    response_id = _identifier(response.get("id"))
    response_model = _identifier(response.get("model"))
    selected = route["selected_endpoints"]
    selected_one = len(selected) == 1
    selected_item = selected[0] if selected_one else {}
    auto_router_stages = [
        stage for stage in route["pipeline"]
        if stage.get("name") == "auto-beta-router"
    ]
    auto_stage_matches = (
        len(auto_router_stages) == 1
        and selected_one
        and auto_router_stages[0].get("resolved_to")
        == selected_item.get("model")
    )
    usage = _usage_summary(response.get("usage"))
    checks = {
        "response_id_present": response_id is not None,
        "response_model_present": response_model is not None,
        "metadata_requested_router_matches": (
            route["requested"] == requested_router
        ),
        "multiple_candidates_observed": (
            isinstance(route["candidate_count"], int)
            and route["candidate_count"] >= 2
        ),
        "one_selected_endpoint": selected_one,
        "auto_router_stage_matches_selected": auto_stage_matches,
        "cost_present": usage.get("cost") is not None,
        **_usage_checks(usage),
    }
    header_generation_id = _identifier(response_headers.get("x-generation-id"))
    if header_generation_id is not None:
        checks["response_header_id_matches"] = header_generation_id == response_id
    if trace is None:
        checks["trace_present"] = False
    else:
        checks.update({
            "trace_present": True,
            "trace_id_matches": trace.get("id") == response_id,
            "trace_request_id_present": bool(trace.get("request_id")),
            "trace_router_matches": trace.get("router") == requested_router,
            "trace_model_matches_selected": (
                selected_one and trace.get("model") == selected_item.get("model")
            ),
            "trace_provider_matches_selected": (
                selected_one
                and trace.get("provider") == selected_item.get("provider")
            ),
            "trace_provider_response_matches": any(
                item.get("model") == selected_item.get("model")
                and item.get("provider") == selected_item.get("provider")
                and item.get("status") == 200
                for item in trace.get("provider_responses", [])
                if isinstance(item, Mapping)
            ),
        })
        response_cost = usage.get("cost")
        trace_cost = trace.get("total_cost")
        if response_cost is not None and trace_cost is not None:
            checks["trace_cost_matches"] = math.isclose(
                float(response_cost), float(trace_cost), rel_tol=1e-9, abs_tol=1e-12
            )
    required = (
        "response_id_present",
        "response_model_present",
        "metadata_requested_router_matches",
        "multiple_candidates_observed",
        "one_selected_endpoint",
        "auto_router_stage_matches_selected",
        "cost_present",
        "usage_present",
        "token_counts_present",
        "token_total_consistent",
        "response_header_id_matches",
        "trace_present",
        "trace_id_matches",
        "trace_request_id_present",
        "trace_router_matches",
        "trace_model_matches_selected",
        "trace_provider_matches_selected",
        "trace_provider_response_matches",
        "trace_cost_matches",
    )
    failures = [name for name in required if checks.get(name) is not True]
    return {
        "status": "reconciled" if not failures else "unverifiable",
        "checks": checks,
        "failures": failures,
    }


def reconcile_concentrate(response: Mapping[str, Any]) -> dict[str, Any]:
    response_id = _identifier(response.get("id"))
    model = _identifier(response.get("model"))
    provider = _provider_from_model(model)
    checks = {
        "response_id_present": response_id is not None,
        "response_model_present": model is not None,
        "provider_qualified_model": provider is not None,
        **_usage_checks(_usage_summary(response.get("usage"))),
        "trace_api_available": False,
    }
    required = (
        "response_id_present",
        "response_model_present",
        "provider_qualified_model",
        "usage_present",
        "token_counts_present",
        "token_total_consistent",
    )
    failures = [name for name in required if checks.get(name) is not True]
    return {
        "status": "observed" if not failures else "unverifiable",
        "checks": checks,
        "failures": failures,
    }


def _case_record(
    *,
    router: str,
    case_id: str,
    repetition: int,
    prompt: str,
    max_output_tokens: int,
    trace_timeout: float,
) -> dict[str, Any]:
    config = _ROUTER_CONFIG[router]
    api_key = os.environ.get(config["api_key_env"])
    if not api_key:
        raise RouterEvidenceError(
            f"{config['api_key_env']} is required to probe {router}"
        )
    correlation_id = str(uuid.uuid4())
    payload = {
        "model": config["requested_router"],
        "input": prompt,
        "max_output_tokens": max_output_tokens,
        "stream": True,
    }
    request_headers = {"X-OpenBench-Request-Id": correlation_id}
    if router == "openrouter":
        request_headers["X-OpenRouter-Metadata"] = "enabled"
    response, response_headers, timings, events = _request_stream(
        endpoint=config["endpoint"],
        api_key=api_key,
        payload=payload,
        headers=request_headers,
    )
    usage = _usage_summary(response.get("usage"))
    response_record = {
        "id": _identifier(response.get("id")),
        "model": _identifier(response.get("model")),
        "provider": _provider_from_model(_identifier(response.get("model"))),
        "status": _identifier(response.get("status")),
        "usage": usage,
        "event_types": events,
        "timings": timings,
    }
    if router == "openrouter":
        route = _openrouter_route(response)
        raw_trace = _poll_openrouter_trace(
            response_record["id"] or "",
            api_key=api_key,
            timeout=trace_timeout,
        )
        trace = _clean_openrouter_trace(raw_trace)
        reconciliation = reconcile_openrouter(
            requested_router=config["requested_router"],
            response=response,
            response_headers=response_headers,
            trace=trace,
        )
        response_record["generation_header_id"] = _identifier(
            response_headers.get("x-generation-id")
        )
    else:
        route = {
            "requested": config["requested_router"],
            "selected_endpoints": [{
                "model": response_record["model"],
                "provider": response_record["provider"],
            }],
            "attempts": [],
            "candidate_count": None,
            "candidate_models": [],
            "pipeline": [],
            "strategy": None,
            "attempt": None,
        }
        trace = None
        reconciliation = reconcile_concentrate(response)
    return {
        "router": router,
        "case_id": case_id,
        "repetition": repetition,
        "correlation_id_hash": hashlib.sha256(
            correlation_id.encode("utf-8")
        ).hexdigest(),
        "request": {
            "endpoint": config["endpoint"],
            "requested_router": config["requested_router"],
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "stream": True,
            "max_output_tokens": max_output_tokens,
        },
        "response": response_record,
        "route": route,
        "trace": trace,
        "reconciliation": reconciliation,
    }


def _walk_forbidden(value: Any, path: str = "$") -> list[str]:
    failures = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold()
            child = f"{path}.{key}"
            if normalized in _FORBIDDEN_ARTIFACT_KEYS:
                failures.append(child)
            failures.extend(_walk_forbidden(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            failures.extend(_walk_forbidden(item, f"{path}[{index}]"))
    return failures


def build_artifact(
    *,
    routers: Iterable[str],
    cases: Iterable[str],
    max_output_tokens: int,
    repetitions: int,
    trace_timeout: float,
) -> dict[str, Any]:
    if max_output_tokens <= 0:
        raise RouterEvidenceError("max_output_tokens must be positive")
    if repetitions <= 0:
        raise RouterEvidenceError("repetitions must be positive")
    records = []
    for case_id in cases:
        prompt = PROBE_CASES[case_id]
        for repetition in range(1, repetitions + 1):
            for router in routers:
                records.append(_case_record(
                    router=router,
                    case_id=case_id,
                    repetition=repetition,
                    prompt=prompt,
                    max_output_tokens=max_output_tokens,
                    trace_timeout=trace_timeout,
                ))
    counts = {"reconciled": 0, "observed": 0, "unverifiable": 0}
    for record in records:
        counts[record["reconciliation"]["status"]] += 1
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "kind": "router_evidence_probe",
        "created_at": _utc_now(),
        "probe_id": str(uuid.uuid4()),
        "native_routing": True,
        "records": records,
        "summary": {
            "records": len(records),
            "evidence_status": counts,
            "routers": sorted(set(routers)),
            "cases": sorted(set(cases)),
            "repetitions": repetitions,
        },
    }
    forbidden = _walk_forbidden(artifact)
    if forbidden:
        raise RouterEvidenceError(
            "artifact contains forbidden sensitive fields: " + ", ".join(forbidden)
        )
    artifact["artifact_sha256"] = _digest(artifact)
    return artifact


def verify_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    checks = {
        "schema_version": value.get("schema_version") == SCHEMA_VERSION,
        "kind": value.get("kind") == "router_evidence_probe",
        "records_present": (
            isinstance(value.get("records"), list) and bool(value["records"])
        ),
        "privacy_safe": not _walk_forbidden(value),
    }
    expected = value.get("artifact_sha256")
    unsigned = dict(value)
    unsigned.pop("artifact_sha256", None)
    checks["artifact_sha256"] = (
        isinstance(expected, str) and expected == _digest(unsigned)
    )
    records = value.get("records") if isinstance(value.get("records"), list) else []
    checks["record_statuses"] = all(
        isinstance(record, Mapping)
        and isinstance(record.get("reconciliation"), Mapping)
        and record["reconciliation"].get("status")
        in {"reconciled", "observed", "unverifiable"}
        for record in records
    )
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "failures": [name for name, passed in checks.items() if not passed],
    }


def _write_artifact(path: str, value: Mapping[str, Any]) -> None:
    destination = pathlib.Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def probe_main(args) -> int:
    routers = args.routers or sorted(SUPPORTED_ROUTERS)
    cases = args.cases or sorted(PROBE_CASES)
    artifact = build_artifact(
        routers=routers,
        cases=cases,
        max_output_tokens=args.max_output_tokens,
        repetitions=args.repetitions,
        trace_timeout=args.trace_timeout,
    )
    verification = verify_artifact(artifact)
    if not verification["ok"]:
        raise RouterEvidenceError(
            "generated artifact failed verification: "
            + ", ".join(verification["failures"])
        )
    _write_artifact(args.output, artifact)
    print(f"router evidence probe: {artifact['summary']['records']} records")
    for status, count in artifact["summary"]["evidence_status"].items():
        print(f"  {status}: {count}")
    print(f"  artifact: {pathlib.Path(args.output).resolve()}")
    return 0 if artifact["summary"]["evidence_status"]["unverifiable"] == 0 else 2


def verify_main(path: str) -> int:
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    verification = verify_artifact(value)
    print("router evidence artifact:", "PASS" if verification["ok"] else "FAIL")
    for name, passed in verification["checks"].items():
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    return 0 if verification["ok"] else 2
