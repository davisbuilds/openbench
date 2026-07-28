"""Direct streaming HTTP execution for Gateway Probe requests."""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import http.client
import ipaddress
import json
import math
import queue
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from . import gateway_metrics, gateway_profiles, gateway_run, gateway_spec
from . import gateway_probe_results as probe_results
from . import gateway_probe_spec as probe_spec
from .gateway_probe_models import GatewayProbeRunError, PrimerError, ProbeBlock


_RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
_RETRYABLE_TRANSPORT_DETAILS = frozenset({
    "connection_reset",
    "connection_closed",
})
_DEFAULT_RETRY_WAIT_S = 1.0
_MAX_RETRY_AFTER_S = 86_400.0


_ROUTE_REASON_STATUS = {
    "missing_stream_metrics": "unverifiable",
    "missing_route_evidence": "unverifiable",
    "missing_requested_model": "unverifiable",
    "missing_served_model": "unverifiable",
    "stream_not_done": "unverifiable",
    "missing_gateway_profile": "unverifiable",
    "missing_cloudflare_metadata": "unverifiable",
    "missing_concentrate_metadata": "unverifiable",
    "missing_openrouter_metadata": "unverifiable",
    "missing_vercel_metadata": "unverifiable",
    "missing_provider": "unverifiable",
    "unqualified_served_model": "unverifiable",
    "missing_metadata_requested_model": "unverifiable",
    "missing_attempt_count": "unverifiable",
    "missing_model_attempts": "unverifiable",
    "missing_successful_attempt": "unverifiable",
    "missing_attempt_evidence": "unverifiable",
    "missing_attempt_provider": "unverifiable",
    "missing_attempt_model": "unverifiable",
    "missing_attempt_status": "unverifiable",
    "malformed_route_evidence": "failed",
    "malformed_events": "failed",
    "fallback_enabled": "failed",
    "served_model_conflict": "failed",
    "provider_conflict": "failed",
    "multiple_attempts": "failed",
    "served_model_not_allowed": "failed",
    "provider_not_allowed": "failed",
    "requested_model_conflict": "failed",
    "malformed_attempts": "failed",
    "fallback_attempt": "failed",
    "attempt_provider_not_allowed": "failed",
    "unsuccessful_attempt": "failed",
}


class _TimedConnection(http.client.HTTPConnection):
    """HTTP connection with separately observed DNS and TCP setup."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float,
        allow_private: bool,
        private_hosts: tuple[str, ...],
        private_cidrs: tuple[str, ...],
    ):
        super().__init__(host, port=port, timeout=timeout)
        self._allow_private = allow_private
        self._private_hosts = private_hosts
        self._private_networks = tuple(ipaddress.ip_network(item) for item in private_cidrs)
        self._request_deadline: float | None = None
        self.phase_s = {"dns_s": None, "tcp_s": None, "tls_s": None}

    def set_request_deadline(self, deadline: float) -> None:
        self._request_deadline = deadline
        if self.sock is not None:
            self.sock.settimeout(self._remaining_timeout())

    def _remaining_timeout(self) -> float:
        if self._request_deadline is None:
            return float(self.timeout)
        remaining = self._request_deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("probe request exceeded its total timeout")
        return max(0.001, remaining)

    def _address_allowed(self, address: str) -> bool:
        parsed = ipaddress.ip_address(address)
        if not self._allow_private:
            return parsed.is_global
        if self.host.lower().rstrip(".") in self._private_hosts:
            return True
        return any(parsed in network for network in self._private_networks)

    def _resolve(self) -> list[tuple[Any, ...]]:
        results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        host, port = self.host, self.port

        def resolve() -> None:
            try:
                value = socket.getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
            except Exception as exc:  # noqa: BLE001 - returned to request thread
                results.put_nowait((False, exc))
            else:
                results.put_nowait((True, value))

        threading.Thread(target=resolve, daemon=True).start()
        try:
            ok, value = results.get(timeout=self._remaining_timeout())
        except queue.Empty as exc:
            raise socket.timeout(
                "probe request exceeded its total timeout"
            ) from exc
        if not ok:
            raise value
        return value

    def _connect_tcp(self) -> socket.socket:
        started = time.monotonic()
        addresses = self._resolve()
        self.phase_s["dns_s"] = time.monotonic() - started
        allowed = [item for item in addresses if self._address_allowed(item[4][0])]
        if not allowed:
            raise GatewayProbeRunError(
                "resolved endpoint addresses violate the endpoint policy"
            )
        last_error: OSError | None = None
        started = time.monotonic()
        for family, socktype, proto, _canonname, sockaddr in allowed:
            candidate = socket.socket(family, socktype, proto)
            try:
                candidate.settimeout(self._remaining_timeout())
                candidate.connect(sockaddr)
                self.phase_s["tcp_s"] = time.monotonic() - started
                return candidate
            except OSError as exc:
                last_error = exc
                candidate.close()
        raise last_error or OSError("no endpoint address could be connected")

    def connect(self) -> None:
        self.sock = self._connect_tcp()
        self.sock.settimeout(self._remaining_timeout())

    def send(self, data: Any) -> None:
        if self.sock is not None:
            self.sock.settimeout(self._remaining_timeout())
        super().send(data)


class _TimedHTTPSConnection(_TimedConnection):
    def __init__(
        self, *args: Any, context: ssl.SSLContext | None = None, **kwargs: Any
    ):
        super().__init__(*args, **kwargs)
        self._context = context or ssl.create_default_context()

    def connect(self) -> None:
        raw = self._connect_tcp()
        started = time.monotonic()
        try:
            raw.settimeout(self._remaining_timeout())
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise
        self.phase_s["tls_s"] = time.monotonic() - started
        self.sock.settimeout(self._remaining_timeout())


def nonce(experiment_digest: str, block: ProbeBlock, role: str) -> str:
    return hashlib.sha256(
        f"{experiment_digest}:{block.case_id}:{block.condition}:"
        f"{block.repetition}:{role}".encode()
    ).hexdigest()[:24]


def request_body(
    prompt: str,
    request_nonce: str,
    plan: gateway_spec.RoutePlan,
    max_output_tokens: int,
) -> bytes:
    content = f"[openbench_probe_nonce:{request_nonce}]\n\n{prompt}"
    if plan.protocol == "openai_chat":
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": content}],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": max_output_tokens,
            "seed": plan.sampling.seed,
        }
    else:
        payload = {
            "input": content,
            "stream": True,
            "store": False,
            "max_output_tokens": max_output_tokens,
        }
    payload.update({
        "model": plan.requested_model,
        "temperature": plan.sampling.temperature,
        "top_p": plan.sampling.top_p,
    })
    gateway_profiles.strip_cache_controls(payload)
    if plan.route_kind == "gateway":
        if plan.gateway is None:
            raise GatewayProbeRunError("gateway route has no gateway profile")
        gateway_profiles.shape_body(
            payload,
            gateway=plan.gateway,
            requested_provider=plan.requested_provider,
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _new_connection(
    plan: gateway_spec.RoutePlan, timeout_s: int
) -> tuple[Any, str]:
    endpoint = urllib.parse.urlsplit(plan.endpoint)
    port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
    cls = _TimedHTTPSConnection if endpoint.scheme == "https" else _TimedConnection
    connection = cls(
        endpoint.hostname,
        port,
        timeout=timeout_s,
        allow_private=plan.allow_private_endpoint,
        private_hosts=plan.private_host_allowlist,
        private_cidrs=plan.private_cidr_allowlist,
    )
    path = endpoint.path or "/"
    if endpoint.query:
        path = f"{path}?{endpoint.query}"
    return connection, path


def _headers(
    plan: gateway_spec.RoutePlan, secret: str, body: bytes
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Content-Length": str(len(body)),
        "Connection": "keep-alive",
    }
    headers.update(
        gateway_profiles.request_headers(
            gateway=plan.gateway,
            gateway_id=plan.gateway_id,
            secret=secret,
        )
    )
    return headers


def _receipt_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
    """Return only bounded, printable receipt identifiers."""
    receipts: dict[str, str] = {}
    duplicates = set()
    for raw_name, raw_value in headers:
        name = raw_name.lower()
        if (
            name not in probe_results.RECEIPT_HEADER_ALLOWLIST
            or name in duplicates
        ):
            continue
        value = raw_value.strip()
        if (
            not value
            or len(value) > probe_results.RECEIPT_VALUE_MAX_LENGTH
            or probe_results.RECEIPT_VALUE_RE.fullmatch(value) is None
        ):
            continue
        if name in receipts:
            receipts.pop(name)
            duplicates.add(name)
            continue
        receipts[name] = value
    return receipts


def _retry_after_seconds(
    headers: list[tuple[str, str]],
    *,
    now: dt.datetime | None = None,
) -> tuple[str, float | None]:
    values = [
        value.strip()
        for name, value in headers
        if name.lower() == "retry-after"
    ]
    if not values:
        return "absent", None
    if len(values) != 1 or not values[0]:
        return "malformed", None
    raw = values[0]
    try:
        seconds = float(raw)
    except ValueError:
        try:
            target = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return "malformed", None
        if target.tzinfo is None:
            target = target.replace(tzinfo=dt.timezone.utc)
        current = now or dt.datetime.now(dt.timezone.utc)
        seconds = (target.astimezone(dt.timezone.utc) - current).total_seconds()
    if not math.isfinite(seconds) or seconds < 0 or seconds > _MAX_RETRY_AFTER_S:
        return "malformed", None
    return "normalized", seconds


def _same_socket(connection: Any, expected: tuple[Any, int, Any]) -> bool:
    socket_object, socket_fileno, socket_peer = expected
    return bool(
        connection.sock is socket_object
        and connection.sock is not None
        and connection.sock.fileno() == socket_fileno
        and connection.sock.getpeername() == socket_peer
    )


def _consume(
    connection: Any,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    plan: gateway_spec.RoutePlan,
    *,
    capture_metrics: bool,
    cold_started_at: float | None = None,
    expected_socket: tuple[Any, int, Any] | None = None,
    progress: dict[str, Any] | None = None,
    absolute_deadline: float | None = None,
) -> tuple[int, dict[str, Any] | None, bool, dict[str, Any]]:
    operation_started_at = time.monotonic()
    deadline = operation_started_at + float(connection.timeout)
    if absolute_deadline is not None:
        deadline = min(deadline, absolute_deadline)
    if operation_started_at >= deadline:
        raise TimeoutError("probe request exceeded its total timeout")
    connection.set_request_deadline(deadline)
    connection.request("POST", path, body=body, headers=dict(headers))
    request_sent_at = time.monotonic()
    if progress is not None:
        progress["request_started_at"] = request_sent_at
    socket_reused = (
        _same_socket(connection, expected_socket)
        if expected_socket is not None
        else None
    )
    if expected_socket is not None and not socket_reused:
        raise PrimerError("warm measured request did not reuse the primer socket")
    if connection.sock is not None:
        connection.sock.settimeout(connection._remaining_timeout())
    response = connection.getresponse()
    response_headers_at = time.monotonic()
    raw_response_headers = response.getheaders()
    response_headers = {key.lower(): value for key, value in raw_response_headers}
    retry_after_status, retry_after_s = _retry_after_seconds(
        raw_response_headers
    )
    if progress is not None:
        progress.update({
            "http_status": response.status,
            "response_headers_at": response_headers_at,
            "retry_after_status": retry_after_status,
            "retry_after_s": retry_after_s,
        })
    parser = None
    if capture_metrics and "text/event-stream" in response_headers.get(
        "content-type", ""
    ).lower():
        parser = gateway_metrics.sse_parser(
            plan.protocol,
            requested_model=plan.requested_model,
            started_at=request_sent_at,
            route_kind=plan.route_kind,
            requested_provider=plan.requested_provider,
            allowed_models=plan.allowed_models,
            allowed_providers=plan.allowed_providers,
            fallback_enabled=plan.fallback_enabled,
            model_match=plan.model_match,
            gateway=plan.gateway,
            response_headers=response_headers,
        )
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("probe request exceeded its total timeout")
        if connection.sock is not None:
            connection.sock.settimeout(connection._remaining_timeout())
        chunk = response.read1(65536)
        if not chunk:
            break
        if parser is not None:
            parser.feed(chunk, time.monotonic())
            if progress is not None:
                snapshot = parser.snapshot()
                progress["semantic_output_started"] = (
                    snapshot["timing"]["semantic_ttft_s"] is not None
                )
                progress["semantic_output_at"] = (
                    None
                    if snapshot["timing"]["semantic_ttft_s"] is None
                    else request_sent_at + snapshot["timing"]["semantic_ttft_s"]
                )
    completed_at = time.monotonic()
    if progress is not None:
        progress["completed_at"] = completed_at
    metrics = parser.finalize(completed_at) if parser is not None else None
    first_body_byte_s = None
    semantic_ttft_s = None
    if isinstance(metrics, Mapping):
        parser_timing = metrics.get("timing")
        if isinstance(parser_timing, Mapping):
            first_body_byte_s = parser_timing.get("ttfb_s")
            semantic_ttft_s = parser_timing.get("semantic_ttft_s")
    timing = {
        "request_to_response_headers_s": response_headers_at - request_sent_at,
        "request_to_first_body_byte_s": first_body_byte_s,
        "request_to_semantic_ttft_s": semantic_ttft_s,
        "request_stream_total_s": completed_at - request_sent_at,
        "cold_end_to_end_response_headers_s": None,
        "cold_end_to_end_first_body_byte_s": None,
        "cold_end_to_end_semantic_ttft_s": None,
        "cold_end_to_end_stream_total_s": None,
    }
    if cold_started_at is not None:
        timing.update({
            "cold_end_to_end_response_headers_s": (
                response_headers_at - cold_started_at
            ),
            "cold_end_to_end_first_body_byte_s": (
                None
                if first_body_byte_s is None
                else request_sent_at - cold_started_at + first_body_byte_s
            ),
            "cold_end_to_end_semantic_ttft_s": (
                None
                if semantic_ttft_s is None
                else request_sent_at - cold_started_at + semantic_ttft_s
            ),
            "cold_end_to_end_stream_total_s": completed_at - cold_started_at,
        })
    evidence = {
        "timing": timing,
        "receipt_headers": _receipt_headers(raw_response_headers),
        "socket_reused": socket_reused,
        "retry_after_status": retry_after_status,
        "retry_after_s": retry_after_s,
    }
    return response.status, metrics, response.will_close, evidence


def _route_status(
    metrics: Mapping[str, Any] | None,
) -> tuple[str, list[str]]:
    if not isinstance(metrics, Mapping):
        return "unverifiable", ["missing_stream_metrics"]
    evidence = metrics.get("route_evidence")
    if not isinstance(evidence, Mapping):
        return "unverifiable", ["missing_route_evidence"]
    reasons = evidence.get("reasons")
    reasons = reasons if isinstance(reasons, list) else ["malformed_route_evidence"]
    normalized_reasons = list(dict.fromkeys(str(reason) for reason in reasons))
    if evidence.get("pass") is True and not normalized_reasons:
        return "verified", []
    statuses = {
        _ROUTE_REASON_STATUS.get(reason, "failed")
        for reason in normalized_reasons
    }
    status = (
        "unverifiable"
        if normalized_reasons and statuses == {"unverifiable"}
        else "failed"
    )
    return status, normalized_reasons


def _completion_stream_evidence(stream: Any) -> dict[str, Any] | None:
    if not isinstance(stream, Mapping):
        return None
    return {
        name: stream.get(name)
        for name in ("done", "terminal_status", "finish_reason", "finalized")
    }


def _transport_error_detail(exc: Exception) -> str:
    if isinstance(exc, http.client.RemoteDisconnected):
        return "connection_closed"
    if isinstance(exc, http.client.BadStatusLine):
        return "bad_status_line"
    if isinstance(exc, http.client.HTTPException):
        return "http_protocol"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, ConnectionResetError):
        return "connection_reset"
    if isinstance(exc, BrokenPipeError):
        return "connection_closed"
    if isinstance(exc, GatewayProbeRunError):
        return "probe_policy"
    if isinstance(exc, OSError):
        return "network_io"
    return "internal"


def _execute_request_once(
    *,
    experiment: probe_spec.GatewayProbeExperiment,
    case: probe_spec.ProbeCase,
    block: ProbeBlock,
    plan: gateway_spec.RoutePlan,
    secret: str,
    prices: Mapping[str, gateway_run.Price],
    remaining_usd_cap: Decimal | None = None,
    request_timeout_s: float | None = None,
    absolute_deadline: float | None = None,
) -> dict[str, Any]:
    attempt_started_at = time.monotonic()
    connection, path = _new_connection(
        plan,
        request_timeout_s or experiment.budget.timeout_s,
    )
    primer_nonce = nonce(experiment.digest, block, "primer")
    measured_nonce = nonce(experiment.digest, block, "measured")
    primer = {
        "required": block.condition == "warm",
        "completed": False,
        "http_status": None,
        "socket_reused": None,
        "primer_nonce_sha256": hashlib.sha256(primer_nonce.encode()).hexdigest(),
        "measured_nonce_sha256": hashlib.sha256(measured_nonce.encode()).hexdigest(),
        "setup": {"dns_s": None, "tcp_s": None, "tls_s": None},
        "receipt_headers": {},
        "route_integrity": None,
        "usage": None,
        "cache": None,
        "costs": {},
        "stream": None,
    }
    status = None
    metrics = None
    error_class = None
    error_detail = None
    timed_out = False
    measured_evidence = None
    measured_attempted = False
    primer_attempted = False
    primer_amount = None
    measured_amount = None
    budget_reason = None
    stop_for_budget = False
    primer_progress: dict[str, Any] = {}
    measured_progress: dict[str, Any] = {}
    active_progress = primer_progress if block.condition == "warm" else measured_progress
    try:
        if block.condition == "warm":
            primer_attempted = True
            body = request_body(
                case.prompt, primer_nonce, plan, experiment.budget.max_output_tokens
            )
            primer_status, primer_metrics, primer_closed, primer_evidence = _consume(
                connection,
                path,
                body,
                _headers(plan, secret, body),
                plan,
                capture_metrics=True,
                progress=primer_progress,
                absolute_deadline=absolute_deadline,
            )
            primer["setup"] = dict(connection.phase_s)
            primer["receipt_headers"] = primer_evidence["receipt_headers"]
            primer_route_status, primer_route_reasons = _route_status(primer_metrics)
            primer["route_integrity"] = {
                "status": primer_route_status,
                "pass": primer_route_status == "verified",
                "reasons": primer_route_reasons,
            }
            primer_stream = (
                primer_metrics.get("stream")
                if isinstance(primer_metrics, Mapping)
                else None
            )
            primer["stream"] = _completion_stream_evidence(primer_stream)
            primer_terminal = (
                primer_stream.get("terminal_status")
                if isinstance(primer_stream, Mapping)
                else None
            )
            primer_stream_completed = (
                isinstance(primer_stream, Mapping)
                and primer_stream.get("done") is True
                and primer_terminal in {None, "completed"}
            )
            if isinstance(primer_metrics, Mapping):
                observed_at = dt.datetime.now(dt.timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                primer_costs, primer_amount = gateway_run.price_call(
                    primer_metrics, prices, plan, observed_at
                )
                primer["usage"] = primer_metrics.get("usage")
                primer["cache"] = gateway_run.cache_accounting(primer_metrics)
                primer["costs"] = primer_costs
            primer.update({
                "completed": (
                    200 <= primer_status < 300
                    and not primer_closed
                    and primer_stream_completed
                    and primer_route_status == "verified"
                ),
                "http_status": primer_status,
            })
            if not primer["completed"] or connection.sock is None:
                raise PrimerError(
                    "warm primer was not successful, route-verified, and reusable"
                )
            primer_socket = (
                connection.sock,
                connection.sock.fileno(),
                connection.sock.getpeername(),
            )
            if primer_amount is None:
                budget_reason = "primer_cost_unavailable"
                stop_for_budget = True
            elif remaining_usd_cap is not None and primer_amount >= remaining_usd_cap:
                budget_reason = "usd_cap_reached_by_primer"
                stop_for_budget = True
        if not stop_for_budget:
            body = request_body(
                case.prompt, measured_nonce, plan, experiment.budget.max_output_tokens
            )
            cold_started_at = (
                time.monotonic() if block.condition == "cold" else None
            )
            measured_attempted = True
            active_progress = measured_progress
            status, metrics, _closed, measured_evidence = _consume(
                connection,
                path,
                body,
                _headers(plan, secret, body),
                plan,
                capture_metrics=True,
                cold_started_at=cold_started_at,
                expected_socket=(
                    primer_socket if block.condition == "warm" else None
                ),
                progress=measured_progress,
                absolute_deadline=absolute_deadline,
            )
            if block.condition == "warm":
                primer["socket_reused"] = measured_evidence["socket_reused"]
    except (socket.timeout, TimeoutError):
        timed_out = True
        error_class = "timeout"
        error_detail = "timeout"
    except PrimerError:
        if block.condition == "warm" and measured_attempted:
            primer["socket_reused"] = False
        error_class = "primer"
        error_detail = "primer_invalid"
    except Exception as exc:  # noqa: BLE001 - classified in the result row
        error_class = "transport"
        error_detail = _transport_error_detail(exc)
    finally:
        connection.close()
    if measured_attempted and status is None:
        progress_status = measured_progress.get("http_status")
        status = progress_status if isinstance(progress_status, int) else None
    semantic_output_started = active_progress.get("semantic_output_started") is True
    route_status, route_reasons = _route_status(metrics)
    costs: dict[str, Any] = {}
    cache = {"cached_input_tokens": None, "cache_write_input_tokens": None}
    if isinstance(metrics, Mapping):
        observed_at = dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        costs, measured_amount = gateway_run.price_call(
            metrics, prices, plan, observed_at
        )
        cache = gateway_run.cache_accounting(metrics)
    if measured_attempted and measured_amount is None:
        budget_reason = "measured_cost_unavailable"
        stop_for_budget = True
    if primer_attempted and primer_amount is None and budget_reason is None:
        budget_reason = "primer_cost_unavailable"
        stop_for_budget = True
    charged_amount = (primer_amount or Decimal(0)) + (measured_amount or Decimal(0))
    if (
        remaining_usd_cap is not None
        and charged_amount >= remaining_usd_cap
        and budget_reason is None
    ):
        budget_reason = "usd_cap_reached"
        stop_for_budget = True
    stream = metrics.get("stream") if isinstance(metrics, Mapping) else None
    terminal = stream.get("terminal_status") if isinstance(stream, Mapping) else None
    stream_done = stream.get("done") is True if isinstance(stream, Mapping) else False
    completed = stream_done and terminal in {None, "completed"}
    if error_class is None and isinstance(status, int) and not 200 <= status < 300:
        error_class = "http"
        error_detail = "http_status"
    elif error_class is None and isinstance(status, int) and not completed:
        error_class = "stream"
        error_detail = (
            "stream_terminal" if terminal is not None else "stream_incomplete"
        )
    elif (
        error_class is None
        and isinstance(status, int)
        and isinstance(measured_evidence, Mapping)
        and measured_evidence["timing"].get(
            "request_to_semantic_ttft_s"
        ) is None
    ):
        error_class = "stream"
        error_detail = "stream_no_semantic_output"
    available = (
        isinstance(status, int)
        and 200 <= status < 300
        and error_class is None
        and completed
    )
    timing = (
        measured_evidence["timing"]
        if isinstance(measured_evidence, Mapping)
        else {
            "request_to_response_headers_s": None,
            "request_to_first_body_byte_s": None,
            "request_to_semantic_ttft_s": None,
            "request_stream_total_s": None,
            "cold_end_to_end_response_headers_s": None,
            "cold_end_to_end_first_body_byte_s": None,
            "cold_end_to_end_semantic_ttft_s": None,
            "cold_end_to_end_stream_total_s": None,
        }
    )
    result = {
        "outcome": {
            "attempted": measured_attempted,
            "success": available,
            "available": available,
            "http_status": status,
            "timed_out": timed_out,
            "error_class": error_class,
            "error_detail": error_detail,
            "budget_exhausted_reason": budget_reason,
        },
        "route_integrity": {
            "status": route_status,
            "pass": route_status == "verified",
            "reasons": route_reasons,
        },
        "request_metrics": {
            "setup": (
                dict(connection.phase_s)
                if block.condition == "cold"
                else None
            ),
            "timing": timing,
            "receipt_headers": (
                measured_evidence["receipt_headers"]
                if isinstance(measured_evidence, Mapping)
                else {}
            ),
            "usage": metrics.get("usage") if isinstance(metrics, Mapping) else None,
            "generation": (
                metrics.get("generation") if isinstance(metrics, Mapping) else None
            ),
            "cache": cache,
            "route": metrics.get("route") if isinstance(metrics, Mapping) else None,
            "costs": costs,
            "stream": stream,
            "coverage": (
                metrics.get("coverage") if isinstance(metrics, Mapping) else None
            ),
        },
        "reuse_evidence": primer,
        "billing": {
            "primer_cost_usd": (
                str(primer_amount) if primer_amount is not None else None
            ),
            "measured_cost_usd": (
                str(measured_amount) if measured_amount is not None else None
            ),
            "charged_cost_usd": str(charged_amount),
            "stop_required": stop_for_budget,
        },
    }
    result["_attempt_meta"] = {
        "attempt_started_at": attempt_started_at,
        "request_started_at": active_progress.get("request_started_at"),
        "response_headers_at": active_progress.get("response_headers_at"),
        "semantic_output_at": active_progress.get("semantic_output_at"),
        "completed_at": active_progress.get("completed_at", time.monotonic()),
        "semantic_output_started": semantic_output_started,
        "retry_after_s": active_progress.get("retry_after_s"),
        "retry_after_status": active_progress.get(
            "retry_after_status",
            "absent",
        ),
        "phase": "measured" if measured_attempted else (
            "primer" if primer_attempted else "measured"
        ),
    }
    return result


def _retryable(result: Mapping[str, Any]) -> bool:
    meta = result["_attempt_meta"]
    if meta["semantic_output_started"]:
        return False
    outcome = result["outcome"]
    status = outcome.get("http_status")
    if meta["phase"] == "primer":
        status = result["reuse_evidence"].get("http_status")
    return bool(
        status in _RETRYABLE_HTTP_STATUSES
        or outcome.get("timed_out") is True
        or (
            outcome.get("error_class") == "transport"
            and outcome.get("error_detail") in _RETRYABLE_TRANSPORT_DETAILS
        )
    )


def _attempt_public_evidence(
    result: Mapping[str, Any],
    *,
    attempt_number: int,
    initial_request_started_at: float,
    retry_eligible: bool,
    reservation_usd: Decimal,
) -> dict[str, Any]:
    meta = result["_attempt_meta"]
    outcome = result["outcome"]
    status = outcome.get("http_status")
    if meta["phase"] == "primer":
        status = result["reuse_evidence"].get("http_status")
    request_started_at = meta.get("request_started_at")
    response_headers_at = meta.get("response_headers_at")
    semantic_output_at = meta.get("semantic_output_at")
    completed_at = meta["completed_at"]
    billing = result["billing"]
    cost_complete = (
        (billing["primer_cost_usd"] is not None or meta["phase"] != "primer")
        and (
            billing["measured_cost_usd"] is not None
            or meta["phase"] != "measured"
        )
    )
    observed_cost = (
        billing["charged_cost_usd"] if cost_complete else None
    )
    budget_debit = (
        Decimal(observed_cost) if observed_cost is not None else reservation_usd
    )
    return {
        "attempt_number": attempt_number,
        "phase": meta["phase"],
        "outcome": {
            "success": outcome["success"],
            "http_status": status,
            "timed_out": outcome["timed_out"],
            "error_class": outcome["error_class"],
            "error_detail": outcome["error_detail"],
            "semantic_output_started": meta["semantic_output_started"],
        },
        "timing": {
            "initial_request_start_offset_s": (
                None
                if not isinstance(request_started_at, (int, float))
                else max(0.0, request_started_at - initial_request_started_at)
            ),
            "request_to_response_headers_s": (
                None
                if not all(
                    isinstance(value, (int, float))
                    for value in (request_started_at, response_headers_at)
                )
                else response_headers_at - request_started_at
            ),
            "request_to_semantic_output_s": (
                None
                if not all(
                    isinstance(value, (int, float))
                    for value in (request_started_at, semantic_output_at)
                )
                else semantic_output_at - request_started_at
            ),
            "attempt_total_s": completed_at - meta["attempt_started_at"],
        },
        "retry": {
            "eligible": retry_eligible,
            "retry_after_status": meta["retry_after_status"],
            "retry_after_s": meta.get("retry_after_s"),
            "wait_requested_s": None,
            "wait_actual_s": None,
            "not_retried_reason": None,
        },
        "cost": {
            "primer_cost_usd": billing["primer_cost_usd"],
            "measured_cost_usd": billing["measured_cost_usd"],
            "observed_cost_usd": observed_cost,
            "known_observed_cost_usd": billing["charged_cost_usd"],
            "budget_debit_usd": str(budget_debit),
            "reservation_usd": str(reservation_usd),
            "cost_status": (
                "observed" if cost_complete else "reserved_unknown"
            ),
        },
    }


def execute_request(
    *,
    experiment: probe_spec.GatewayProbeExperiment,
    case: probe_spec.ProbeCase,
    block: ProbeBlock,
    plan: gateway_spec.RoutePlan,
    secret: str,
    prices: Mapping[str, gateway_run.Price],
    remaining_usd_cap: Decimal | None = None,
) -> dict[str, Any]:
    attempt_results = []
    attempts = []
    known_observed = Decimal(0)
    budget_debit = Decimal(0)
    price = prices.get(plan.canonical_model)
    call_reservation = (
        Decimal(0)
        if price is None
        else (
            Decimal(experiment.budget.max_input_tokens or 0)
            * price.input_per_million
            + Decimal(experiment.budget.max_output_tokens)
            * price.output_per_million
        )
        / Decimal(1_000_000)
    )
    attempt_reservation = call_reservation * (
        2 if block.condition == "warm" else 1
    )
    initial_request_started_at = None
    result = None
    retry_deadline = None
    for attempt_number in range(1, experiment.budget.max_total_attempts + 1):
        request_timeout_s = None
        if retry_deadline is not None:
            remaining_retry_s = retry_deadline - time.monotonic()
            if remaining_retry_s <= 0:
                attempts[-1]["retry"]["retry_after_status"] = "over_deadline"
                attempts[-1]["retry"]["not_retried_reason"] = "deadline"
                break
            request_timeout_s = min(
                float(experiment.budget.timeout_s),
                remaining_retry_s,
            )
        per_attempt_cap = (
            None
            if remaining_usd_cap is None
            else max(Decimal(0), remaining_usd_cap - budget_debit)
        )
        result = _execute_request_once(
            experiment=experiment,
            case=case,
            block=block,
            plan=plan,
            secret=secret,
            prices=prices,
            remaining_usd_cap=per_attempt_cap,
            request_timeout_s=request_timeout_s,
            absolute_deadline=retry_deadline,
        )
        meta = result["_attempt_meta"]
        request_started_at = meta.get("request_started_at")
        if initial_request_started_at is None:
            if (
                block.condition == "warm"
                and meta["phase"] == "primer"
            ):
                initial_request_started_at = meta["completed_at"]
            else:
                initial_request_started_at = (
                    request_started_at
                    if isinstance(request_started_at, (int, float))
                    else meta["attempt_started_at"]
                )
            retry_deadline = (
                initial_request_started_at
                + float(experiment.budget.retry_deadline_s or 0)
                if experiment.budget.retry_deadline_s is not None
                else None
            )
        eligible = _retryable(result)
        evidence = _attempt_public_evidence(
            result,
            attempt_number=attempt_number,
            initial_request_started_at=initial_request_started_at,
            retry_eligible=eligible,
            reservation_usd=attempt_reservation,
        )
        attempts.append(evidence)
        attempt_results.append(result)
        known_observed += Decimal(
            evidence["cost"]["known_observed_cost_usd"]
        )
        budget_debit += Decimal(evidence["cost"]["budget_debit_usd"])
        if (
            not eligible
            or attempt_number == experiment.budget.max_total_attempts
            or (
                remaining_usd_cap is not None
                and budget_debit + attempt_reservation > remaining_usd_cap
            )
        ):
            if not eligible:
                evidence["retry"]["not_retried_reason"] = (
                    "semantic_output_started"
                    if meta["semantic_output_started"]
                    else "not_retryable"
                )
            elif attempt_number == experiment.budget.max_total_attempts:
                evidence["retry"]["not_retried_reason"] = "attempt_limit"
            else:
                evidence["retry"]["not_retried_reason"] = "budget"
            break
        if meta["retry_after_status"] == "malformed":
            evidence["retry"]["not_retried_reason"] = "malformed_retry_after"
            break
        wait_requested = (
            meta["retry_after_s"]
            if isinstance(meta.get("retry_after_s"), (int, float))
            else _DEFAULT_RETRY_WAIT_S
        )
        if (
            retry_deadline is not None
            and time.monotonic() + wait_requested > retry_deadline
        ):
            evidence["retry"]["retry_after_status"] = "over_deadline"
            evidence["retry"]["not_retried_reason"] = "deadline"
            break
        wait_started = time.monotonic()
        time.sleep(wait_requested)
        wait_actual = time.monotonic() - wait_started
        evidence["retry"]["wait_requested_s"] = wait_requested
        evidence["retry"]["wait_actual_s"] = wait_actual

    assert result is not None
    final_meta = result.pop("_attempt_meta")
    for prior in attempt_results[:-1]:
        prior.pop("_attempt_meta", None)
    unknown_cost_attempts = sum(
        attempt["cost"]["cost_status"] == "reserved_unknown"
        for attempt in attempts
    )
    final_retryable_unknown = (
        attempts[-1]["retry"]["eligible"]
        and attempts[-1]["cost"]["cost_status"] == "reserved_unknown"
        and experiment.budget.max_input_tokens is not None
    )
    original_stop_required = result["billing"]["stop_required"]
    original_budget_reason = result["outcome"]["budget_exhausted_reason"]
    if final_retryable_unknown and original_budget_reason in {
        "primer_cost_unavailable",
        "measured_cost_unavailable",
    }:
        original_stop_required = False
        original_budget_reason = None
    if attempts[-1]["retry"]["not_retried_reason"] == "budget":
        original_stop_required = True
        original_budget_reason = "usd_cap_reached"
    result["outcome"]["budget_exhausted_reason"] = original_budget_reason
    result["billing"] = {
        "primer_cost_usd": _sum_known_costs(
            attempts, "primer_cost_usd"
        ),
        "measured_cost_usd": _sum_known_costs(
            attempts, "measured_cost_usd"
        ),
        "charged_cost_usd": (
            str(known_observed) if unknown_cost_attempts == 0 else None
        ),
        "observed_cost_usd": (
            str(known_observed) if unknown_cost_attempts == 0 else None
        ),
        "known_observed_cost_usd": str(known_observed),
        "budget_debit_usd": str(budget_debit),
        "cost_status": (
            "observed" if unknown_cost_attempts == 0 else "reserved_unknown"
        ),
        "unknown_cost_attempts": unknown_cost_attempts,
        "stop_required": original_stop_required,
    }
    final_request_started_at = final_meta.get("request_started_at")
    result["retry_evidence"] = {
        "max_total_attempts": experiment.budget.max_total_attempts,
        "max_input_tokens": experiment.budget.max_input_tokens,
        "max_output_tokens": experiment.budget.max_output_tokens,
        "retry_deadline_s": experiment.budget.retry_deadline_s,
        "reservation_input_per_million_usd": (
            str(price.input_per_million) if price is not None else "0"
        ),
        "reservation_output_per_million_usd": (
            str(price.output_per_million) if price is not None else "0"
        ),
        "attempt_count": len(attempts),
        "recovered": len(attempts) > 1 and result["outcome"]["success"],
        "first_attempt_outcome": dict(attempts[0]["outcome"]),
        "eventual_outcome": dict(attempts[-1]["outcome"]),
        "recovery_timing": {
            "initial_request_to_final_response_headers_s": _elapsed_from(
                initial_request_started_at,
                final_meta.get("response_headers_at"),
            ),
            "initial_request_to_final_semantic_output_s": _elapsed_from(
                initial_request_started_at,
                final_meta.get("semantic_output_at"),
            ),
            "initial_request_to_completion_s": _elapsed_from(
                initial_request_started_at,
                final_meta.get("completed_at"),
            ),
            "final_attempt_request_start_offset_s": _elapsed_from(
                initial_request_started_at,
                final_request_started_at,
            ),
        },
        "attempts": attempts,
    }
    return result


def _elapsed_from(start: Any, end: Any) -> float | None:
    if not all(isinstance(value, (int, float)) for value in (start, end)):
        return None
    return max(0.0, end - start)


def _sum_known_costs(
    attempts: list[Mapping[str, Any]],
    field: str,
) -> str | None:
    values = [
        Decimal(value)
        for attempt in attempts
        if (value := attempt["cost"][field]) is not None
    ]
    return str(sum(values, Decimal(0))) if values else None
