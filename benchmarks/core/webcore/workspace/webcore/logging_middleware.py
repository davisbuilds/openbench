"""webcore.logging_middleware -- request/response logging middleware.

A worked example of the middleware contract (``mw(request, next) -> response``)
that times each request, formats a one-line access record, and hands it to a
sink. It doubles as a template for writing your own cross-cutting middleware.

The formatting is pluggable (:class:`AccessLogFormatter`) and the sink defaults
to a callable you supply (e.g. ``print`` or a ``logging.Logger.info``), so the
middleware itself does no I/O policy.

Example
-------
::

    records = []
    app.use(RequestLogger(sink=records.append))
"""

from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

__all__ = [
    "RequestLogger",
    "AccessLogFormatter",
    "TimingMiddleware",
    "RequestIDMiddleware",
]


class AccessLogFormatter:
    """Render an access-log line from a request/response/duration triple.

    The default template mirrors a common combined-log shape::

        GET /users/7 -> 200 (1.42ms)

    Override :meth:`format` or pass a ``template`` with the fields ``method``,
    ``path``, ``status``, ``ms``, and ``length``.
    """

    DEFAULT_TEMPLATE = "{method} {path} -> {status} ({ms:.2f}ms)"

    def __init__(self, template: Optional[str] = None) -> None:
        self.template = template or self.DEFAULT_TEMPLATE

    def format(self, request, response, duration_ms: float) -> str:
        length = len(response.body_bytes()) if hasattr(response, "body_bytes") else 0
        return self.template.format(
            method=request.method,
            path=request.full_path,
            status=getattr(response, "status", "-"),
            ms=duration_ms,
            length=length,
        )


class RequestLogger:
    """Middleware that logs one line per request after the handler returns.

    Parameters
    ----------
    sink:
        A callable that receives each formatted line (default: :func:`print`).
    formatter:
        An :class:`AccessLogFormatter`; a default one is used if omitted.
    slow_ms:
        If set, requests slower than this many milliseconds are also passed to
        :attr:`slow_sink` (defaulting to ``sink``) so slow requests can be
        surfaced separately.
    """

    def __init__(self, sink: Callable[[str], Any] = print,
                 formatter: Optional[AccessLogFormatter] = None,
                 slow_ms: Optional[float] = None,
                 slow_sink: Optional[Callable[[str], Any]] = None,
                 time_func: Callable[[], float] = time.perf_counter) -> None:
        self.sink = sink
        self.formatter = formatter or AccessLogFormatter()
        self.slow_ms = slow_ms
        self.slow_sink = slow_sink or sink
        self._time = time_func
        self.count = 0

    def __call__(self, request, next_call):
        start = self._time()
        try:
            response = next_call(request)
        except Exception:
            duration_ms = (self._time() - start) * 1000.0
            self.sink("{} {} -> EXC ({:.2f}ms)".format(
                request.method, request.full_path, duration_ms))
            raise
        duration_ms = (self._time() - start) * 1000.0
        self.count += 1
        line = self.formatter.format(request, response, duration_ms)
        self.sink(line)
        if self.slow_ms is not None and duration_ms >= self.slow_ms:
            self.slow_sink("SLOW " + line)
        return response

    def __repr__(self) -> str:
        return "<RequestLogger count={}>".format(self.count)


class TimingMiddleware:
    """Middleware that records each request's duration on a response header.

    Adds ``X-Response-Time: <ms>ms`` so callers (and tests) can observe timing
    without a separate log sink. A rolling list of the most recent durations is
    kept on :attr:`samples` for lightweight profiling.
    """

    def __init__(self, header: str = "X-Response-Time", keep: int = 100,
                 time_func: Callable[[], float] = time.perf_counter) -> None:
        self.header = header
        self.keep = keep
        self._time = time_func
        self.samples: List[float] = []

    def __call__(self, request, next_call):
        start = self._time()
        response = next_call(request)
        duration_ms = (self._time() - start) * 1000.0
        self._record(duration_ms)
        if hasattr(response, "headers"):
            response.headers[self.header] = "{:.2f}ms".format(duration_ms)
        return response

    def _record(self, duration_ms: float) -> None:
        self.samples.append(duration_ms)
        if len(self.samples) > self.keep:
            del self.samples[0]

    @property
    def average_ms(self) -> float:
        """Mean of the retained samples (0.0 when empty)."""
        return sum(self.samples) / len(self.samples) if self.samples else 0.0

    def __repr__(self) -> str:
        return "<TimingMiddleware avg={:.2f}ms>".format(self.average_ms)


class RequestIDMiddleware:
    """Attach a per-request id to the request and echo it on the response.

    Reuses an incoming id header when present (so an upstream proxy's id is
    preserved), otherwise mints a short counter-based one. The id is stored on
    the request as ``request.request_id`` for handlers and log lines to use.
    """

    def __init__(self, header: str = "X-Request-ID", prefix: str = "req") -> None:
        self.header = header
        self.prefix = prefix
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return "{}-{:06d}".format(self.prefix, self._counter)

    def __call__(self, request, next_call):
        incoming = request.header(self.header)
        request_id = incoming or self._next_id()
        setattr(request, "request_id", request_id)
        response = next_call(request)
        if hasattr(response, "headers"):
            response.headers[self.header] = request_id
        return response

    def __repr__(self) -> str:
        return "<RequestIDMiddleware header={!r}>".format(self.header)
