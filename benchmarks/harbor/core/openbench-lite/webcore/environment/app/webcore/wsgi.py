"""webcore.wsgi -- a WSGI adapter around an :class:`~webcore.app.App`.

webcore has no network layer of its own -- it is driven in-process by
:class:`~webcore.testclient.TestClient`. This module bridges that gap for anyone
who wants to serve a webcore app behind a real WSGI server
(:mod:`wsgiref.simple_server`, gunicorn, etc.) without pulling in a dependency.

:class:`WSGIAdapter` translates a WSGI ``environ`` into a
:class:`~webcore.request.Request`, calls :meth:`App.dispatch`, and streams the
resulting :class:`~webcore.response.Response` back through ``start_response``.
:func:`build_request` and :func:`response_to_wsgi` are exposed separately so the
translation can be tested in isolation.

Example
-------
::

    from wsgiref.simple_server import make_server
    from webcore.wsgi import WSGIAdapter

    app = App()
    server = make_server("127.0.0.1", 8000, WSGIAdapter(app))
    server.serve_forever()
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Tuple
from urllib.parse import quote

from .request import Request

__all__ = [
    "WSGIAdapter",
    "build_request",
    "response_to_wsgi",
    "environ_headers",
]

#: WSGI environ keys that hold header values without the ``HTTP_`` prefix.
_UNPREFIXED = {"CONTENT_TYPE": "Content-Type", "CONTENT_LENGTH": "Content-Length"}


def environ_headers(environ: Dict[str, Any]) -> Dict[str, str]:
    """Extract HTTP request headers from a WSGI ``environ``.

    ``HTTP_X_FOO`` becomes ``X-Foo``; the two un-prefixed exceptions
    (``CONTENT_TYPE``/``CONTENT_LENGTH``) are mapped back to their header names.
    """
    headers: Dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").title()
            headers[name] = value
        elif key in _UNPREFIXED and value:
            headers[_UNPREFIXED[key]] = value
    return headers


def _read_body(environ: Dict[str, Any]) -> bytes:
    """Read the request body from ``wsgi.input`` up to ``CONTENT_LENGTH``."""
    stream = environ.get("wsgi.input")
    if stream is None:
        return b""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return b""
    data = stream.read(length)
    return data if isinstance(data, bytes) else str(data).encode("utf-8")


def build_request(environ: Dict[str, Any]) -> Request:
    """Construct a webcore :class:`Request` from a WSGI ``environ``.

    Reassembles the request target from ``PATH_INFO`` (percent-encoded per
    segment) and ``QUERY_STRING``, collects headers, and reads the body.
    """
    method = environ.get("REQUEST_METHOD", "GET")
    raw_path = environ.get("PATH_INFO", "") or "/"
    # PATH_INFO is already decoded by the server; re-quote so Request's own
    # splitting/decoding sees a conventional target.
    path = quote(raw_path, safe="/%")
    query = environ.get("QUERY_STRING", "")
    target = path + ("?" + query if query else "")
    headers = environ_headers(environ)
    body = _read_body(environ)
    return Request(method, target, headers, body)


def response_to_wsgi(response, start_response: Callable) -> List[bytes]:
    """Emit ``response`` through ``start_response`` and return the body iterable.

    The status line is ``"<code> <reason>"``; headers come straight from the
    response's :class:`~webcore.response.Headers` map, preserving order and
    original casing.
    """
    status_line = "{} {}".format(response.status, response.reason).rstrip()
    header_list: List[Tuple[str, str]] = list(response.headers.items())
    start_response(status_line, header_list)
    return [response.body_bytes()]


class WSGIAdapter:
    """Make an :class:`~webcore.app.App` callable as a WSGI application.

    Parameters
    ----------
    app:
        The webcore application to serve.
    catch_errors:
        When true (default), an unhandled exception during dispatch is turned
        into a ``500`` response instead of propagating into the server.
    """

    def __init__(self, app: Any, catch_errors: bool = True) -> None:
        self.app = app
        self.catch_errors = catch_errors

    def __call__(self, environ: Dict[str, Any],
                 start_response: Callable) -> Iterable[bytes]:
        request = build_request(environ)
        try:
            response = self.app.dispatch(request)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            if not self.catch_errors:
                raise
            return self._error(start_response, exc)
        return response_to_wsgi(response, start_response)

    def _error(self, start_response: Callable, exc: Exception) -> List[bytes]:
        body = "500 Internal Server Error".encode("utf-8")
        start_response(
            "500 Internal Server Error",
            [("Content-Type", "text/plain; charset=utf-8"),
             ("Content-Length", str(len(body)))],
        )
        return [body]

    def __repr__(self) -> str:
        return "<WSGIAdapter {!r}>".format(getattr(self.app, "name", self.app))
