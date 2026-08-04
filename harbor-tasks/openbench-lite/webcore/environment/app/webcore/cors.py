"""webcore.cors -- Cross-Origin Resource Sharing middleware.

Browsers gate cross-origin requests behind CORS: a preflight ``OPTIONS`` asks
whether a real request would be allowed, and the server answers with
``Access-Control-*`` headers. This middleware implements that handshake with
configurable allowed origins, methods, and headers, and adds the appropriate
headers to normal responses too.

It is standalone (``app.use(CORS(...))``) and, for a preflight, short-circuits
with a ``204`` carrying the negotiated headers -- no handler runs.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from .response import Response

__all__ = ["CORS", "is_preflight"]

#: Methods allowed by default for a permissive configuration.
_DEFAULT_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


def is_preflight(request) -> bool:
    """True if ``request`` is a CORS preflight (``OPTIONS`` + request-method hint)."""
    return (
        request.method == "OPTIONS"
        and request.header("access-control-request-method") is not None
    )


class CORS:
    """Middleware that applies CORS headers and answers preflights.

    Parameters
    ----------
    allow_origins:
        Allowed origins; ``"*"`` (the default) allows any. An explicit list is
        echoed back only when the request's ``Origin`` is a member.
    allow_methods:
        Methods advertised on preflight (default: the common verb set).
    allow_headers:
        Request headers a client may send; ``"*"`` mirrors whatever the preflight
        requests.
    allow_credentials:
        Whether to emit ``Access-Control-Allow-Credentials: true`` (which forbids
        a wildcard origin, so the concrete origin is echoed instead).
    max_age:
        Seconds a preflight result may be cached by the browser.
    """

    def __init__(self, allow_origins: Sequence[str] = ("*",),
                 allow_methods: Sequence[str] = _DEFAULT_METHODS,
                 allow_headers: Sequence[str] = ("*",),
                 allow_credentials: bool = False,
                 expose_headers: Sequence[str] = (),
                 max_age: int = 600) -> None:
        self.allow_origins = list(allow_origins)
        self.allow_methods = list(allow_methods)
        self.allow_headers = list(allow_headers)
        self.allow_credentials = allow_credentials
        self.expose_headers = list(expose_headers)
        self.max_age = max_age

    def _resolve_origin(self, origin: Optional[str]) -> Optional[str]:
        if origin is None:
            return None
        if "*" in self.allow_origins:
            # Credentials mode cannot use "*"; echo the concrete origin.
            return origin if self.allow_credentials else "*"
        return origin if origin in self.allow_origins else None

    def _apply_common(self, response, origin: Optional[str]) -> None:
        allow_origin = self._resolve_origin(origin)
        if allow_origin is None:
            return
        response.headers["Access-Control-Allow-Origin"] = allow_origin
        if allow_origin != "*":
            response.headers["Vary"] = "Origin"
        if self.allow_credentials:
            response.headers["Access-Control-Allow-Credentials"] = "true"
        if self.expose_headers:
            response.headers["Access-Control-Expose-Headers"] = ", ".join(self.expose_headers)

    def _preflight_response(self, request) -> Response:
        origin = request.header("origin")
        resp = Response(204, None, b"")
        self._apply_common(resp, origin)
        resp.headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods)
        requested_headers = request.header("access-control-request-headers")
        if "*" in self.allow_headers and requested_headers:
            resp.headers["Access-Control-Allow-Headers"] = requested_headers
        elif self.allow_headers and "*" not in self.allow_headers:
            resp.headers["Access-Control-Allow-Headers"] = ", ".join(self.allow_headers)
        resp.headers["Access-Control-Max-Age"] = str(self.max_age)
        return resp

    def __call__(self, request, next_call):
        if is_preflight(request):
            return self._preflight_response(request)
        response = next_call(request)
        if hasattr(response, "headers"):
            self._apply_common(response, request.header("origin"))
        return response

    def __repr__(self) -> str:
        return "<CORS origins={}>".format(self.allow_origins)
