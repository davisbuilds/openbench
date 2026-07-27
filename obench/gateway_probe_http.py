"""Direct streaming HTTP execution for Gateway Probe requests."""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import ipaddress
import json
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
) -> tuple[int, dict[str, Any] | None, bool, dict[str, Any]]:
    operation_started_at = time.monotonic()
    deadline = operation_started_at + float(connection.timeout)
    connection.set_request_deadline(deadline)
    connection.request("POST", path, body=body, headers=dict(headers))
    request_sent_at = time.monotonic()
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
    completed_at = time.monotonic()
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


def _transport_error_detail(exc: Exception) -> str:
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
    connection, path = _new_connection(plan, experiment.budget.timeout_s)
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
    return {
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
