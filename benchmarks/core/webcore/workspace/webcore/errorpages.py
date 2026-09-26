"""webcore.errorpages -- default error rendering and handler registration.

When a handler raises an :class:`~webcore.exceptions.HTTPException` (or any
exception), something has to turn that into a presentable
:class:`~webcore.response.Response`. This module supplies:

*   :class:`ErrorRegistry` -- map status codes (or exception classes) to custom
    renderers, with a sensible default for everything else.
*   default renderers that content-negotiate between an HTML page and a JSON
    body based on the request's ``Accept`` header.

It is intentionally standalone: an application opts in by consulting the registry
in a middleware or handler wrapper; nothing here changes core dispatch.

Example
-------
::

    errors = ErrorRegistry()

    @errors.handler(404)
    def not_found(request, exc):
        return html("<h1>nothing here</h1>", status=404)

    resp = errors.render(request, NotFound())
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Type

from .exceptions import HTTPException
from .negotiation import Accept
from .response import Response, html, json_response, text
from .status import is_error, reason_phrase
from .templating import Environment

__all__ = [
    "ErrorRegistry",
    "render_html_error",
    "render_json_error",
    "render_error",
    "DEFAULT_ERROR_TEMPLATE",
]


#: The default HTML shell for an error page, rendered by :mod:`webcore.templating`.
DEFAULT_ERROR_TEMPLATE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{{ code }} {{ title }}</title></head>
<body>
  <h1>{{ code }} {{ title }}</h1>
  <p>{{ description }}</p>
  {% if show_detail %}<pre>{{ detail }}</pre>{% endif %}
  <hr>
  <small>webcore</small>
</body>
</html>
"""

_ENV = Environment()


def _exception_fields(exc: BaseException, status_code: int) -> Dict[str, Any]:
    """Normalise any exception into the fields a renderer needs."""
    if isinstance(exc, HTTPException):
        description = exc.description
    else:
        description = reason_phrase(status_code) or "Error"
    return {
        "code": status_code,
        "title": reason_phrase(status_code) or "Error",
        "description": description,
        "detail": "{}: {}".format(type(exc).__name__, exc),
    }


def render_html_error(request, exc: BaseException, status_code: int,
                      show_detail: bool = False) -> Response:
    """Render ``exc`` as an HTML error page (via the templating engine)."""
    fields = _exception_fields(exc, status_code)
    fields["show_detail"] = show_detail
    body = _ENV.render_string(DEFAULT_ERROR_TEMPLATE, **fields)
    resp = html(body, status=status_code)
    _copy_exception_headers(exc, resp)
    return resp


def render_json_error(request, exc: BaseException, status_code: int,
                      show_detail: bool = False) -> Response:
    """Render ``exc`` as a JSON error body (``{"error": ..., "status": ...}``)."""
    fields = _exception_fields(exc, status_code)
    payload: Dict[str, Any] = {
        "error": fields["title"],
        "status": status_code,
        "message": fields["description"],
    }
    if show_detail:
        payload["detail"] = fields["detail"]
    resp = json_response(payload, status=status_code)
    _copy_exception_headers(exc, resp)
    return resp


def _copy_exception_headers(exc: BaseException, resp: Response) -> None:
    """Carry an HTTPException's extra headers (e.g. ``Allow``) onto the response."""
    headers = getattr(exc, "headers", None)
    if headers:
        for key, value in headers.items():
            resp.headers[key] = value


def render_error(request, exc: BaseException, status_code: int,
                 show_detail: bool = False) -> Response:
    """Content-negotiate an error response between JSON and HTML.

    If the request's ``Accept`` header prefers ``application/json`` (or JSON
    outranks HTML), a JSON body is returned; otherwise an HTML page. With no
    usable ``Accept`` header, plain text is used as the neutral default.
    """
    accept = Accept.from_header(request.header("accept", ""), media=True)
    if not accept:
        fields = _exception_fields(exc, status_code)
        resp = text("{} {}".format(status_code, fields["description"]), status=status_code)
        _copy_exception_headers(exc, resp)
        return resp
    best = accept.best_match(["text/html", "application/json"], default="text/html")
    if best == "application/json":
        return render_json_error(request, exc, status_code, show_detail)
    return render_html_error(request, exc, status_code, show_detail)


class ErrorRegistry:
    """A lookup from status code / exception type to a rendering callable.

    A registered handler has the signature ``handler(request, exc) -> Response``.
    Lookup prefers, in order: an exact exception-type handler, an exact
    status-code handler, then the registry default (:func:`render_error`).
    """

    def __init__(self, default: Optional[Callable] = None,
                 show_detail: bool = False) -> None:
        self._by_code: Dict[int, Callable] = {}
        self._by_type: Dict[Type[BaseException], Callable] = {}
        self._default = default or render_error
        self.show_detail = show_detail

    def register(self, key, handler: Callable) -> None:
        """Register ``handler`` for a status-code ``int`` or an exception class."""
        if isinstance(key, int):
            self._by_code[key] = handler
        elif isinstance(key, type) and issubclass(key, BaseException):
            self._by_type[key] = handler
        else:
            raise TypeError("key must be a status code or exception class")

    def handler(self, key) -> Callable:
        """Decorator form of :meth:`register`."""
        def decorator(func: Callable) -> Callable:
            self.register(key, func)
            return func
        return decorator

    def status_code_for(self, exc: BaseException) -> int:
        """The HTTP status an exception maps to (500 for non-HTTP exceptions)."""
        code = getattr(exc, "status_code", None)
        if isinstance(code, int) and is_error(code):
            return code
        return 500

    def lookup(self, exc: BaseException, status_code: int) -> Callable:
        """Resolve the most specific registered handler for ``exc``."""
        for klass in type(exc).__mro__:
            if klass in self._by_type:
                return self._by_type[klass]
        if status_code in self._by_code:
            return self._by_code[status_code]
        return self._default

    def render(self, request, exc: BaseException) -> Response:
        """Turn ``exc`` into a :class:`Response` using the best-matching handler."""
        status_code = self.status_code_for(exc)
        handler = self.lookup(exc, status_code)
        if handler is self._default:
            return handler(request, exc, status_code, self.show_detail)
        return handler(request, exc)

    def __repr__(self) -> str:
        return "<ErrorRegistry codes={} types={}>".format(
            sorted(self._by_code), [t.__name__ for t in self._by_type]
        )
