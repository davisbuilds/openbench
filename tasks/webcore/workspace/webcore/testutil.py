"""webcore.testutil -- extra helpers for exercising an app in tests.

:class:`~webcore.testclient.TestClient` already drives an app in memory; this
module layers ergonomics on top: fluent assertions over a
:class:`~webcore.response.Response` (:class:`ResponseAssert`), request builders
that set common headers/bodies for you (JSON, form, cookies), and a
:class:`CapturingMiddleware` that records the requests flowing through an app so a
test can assert on ordering and short-circuiting.

None of this is imported by the framework itself; it exists purely to make
application test suites terser. It is deliberately assertion-library-agnostic --
failures raise :class:`AssertionError` with a readable message.
"""

from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .request import Request

__all__ = [
    "ResponseAssert",
    "assert_that",
    "json_request_args",
    "form_request_args",
    "cookie_header",
    "CapturingMiddleware",
    "make_request",
]


def cookie_header(cookies: Dict[str, str]) -> str:
    """Render a ``dict`` of cookies into a ``Cookie:`` header value."""
    return "; ".join("{}={}".format(name, value) for name, value in cookies.items())


def json_request_args(data: Any, **extra_headers: str) -> Dict[str, Any]:
    """Build ``request(...)`` kwargs for a JSON body.

    Returns ``{"headers": {...}, "body": b"..."}`` with a
    ``Content-Type: application/json`` header, ready to splat into a
    :class:`TestClient` call.
    """
    headers = {"Content-Type": "application/json"}
    headers.update(extra_headers)
    body = _json.dumps(data, sort_keys=True).encode("utf-8")
    return {"headers": headers, "body": body}


def form_request_args(fields: Dict[str, Any], **extra_headers: str) -> Dict[str, Any]:
    """Build ``request(...)`` kwargs for a urlencoded form body."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    headers.update(extra_headers)
    body = urlencode(fields).encode("utf-8")
    return {"headers": headers, "body": body}


def make_request(method: str, path: str, *, json: Any = None,
                 form: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None,
                 cookies: Optional[Dict[str, str]] = None) -> Request:
    """Construct a :class:`Request` with common conveniences pre-applied.

    Exactly one of ``json``/``form`` may set the body and its content type;
    ``cookies`` is folded into a ``Cookie`` header. Handy for unit-testing a
    handler without an :class:`App`.
    """
    final_headers: Dict[str, str] = dict(headers or {})
    body: Any = b""
    if json is not None and form is not None:
        raise ValueError("pass json or form, not both")
    if json is not None:
        final_headers.setdefault("Content-Type", "application/json")
        body = _json.dumps(json, sort_keys=True).encode("utf-8")
    elif form is not None:
        final_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        body = urlencode(form).encode("utf-8")
    if cookies:
        final_headers.setdefault("Cookie", cookie_header(cookies))
    return Request(method, path, final_headers, body)


class ResponseAssert:
    """A fluent wrapper for asserting on a :class:`Response`.

    Every check returns ``self`` so they chain::

        assert_that(resp).ok().header("Content-Type", "application/json") \\
            .json_equals({"id": 7})

    A failed check raises :class:`AssertionError` with a message that includes
    the offending status/header/body.
    """

    def __init__(self, response) -> None:
        self.response = response

    # -- status ----------------------------------------------------------

    def status(self, expected: int) -> "ResponseAssert":
        actual = self.response.status
        if actual != expected:
            raise AssertionError("expected status {}, got {}".format(expected, actual))
        return self

    def ok(self) -> "ResponseAssert":
        """Assert a 2xx status."""
        if not 200 <= self.response.status < 300:
            raise AssertionError("expected 2xx, got {}".format(self.response.status))
        return self

    def redirect(self, location: Optional[str] = None) -> "ResponseAssert":
        """Assert a 3xx status and, optionally, an exact ``Location``."""
        if not 300 <= self.response.status < 400:
            raise AssertionError("expected 3xx, got {}".format(self.response.status))
        if location is not None:
            self.header("Location", location)
        return self

    def not_found(self) -> "ResponseAssert":
        return self.status(404)

    # -- headers ---------------------------------------------------------

    def header(self, name: str, expected: Optional[str] = None) -> "ResponseAssert":
        """Assert a header is present and, if given, equals ``expected``."""
        if name not in self.response.headers:
            raise AssertionError("missing header {!r}".format(name))
        if expected is not None:
            actual = self.response.headers.get(name)
            if actual != expected:
                raise AssertionError(
                    "header {!r}: expected {!r}, got {!r}".format(name, expected, actual))
        return self

    def header_contains(self, name: str, fragment: str) -> "ResponseAssert":
        """Assert a header exists and contains ``fragment`` as a substring."""
        value = self.response.headers.get(name, "")
        if fragment not in value:
            raise AssertionError(
                "header {!r} {!r} does not contain {!r}".format(name, value, fragment))
        return self

    # -- body ------------------------------------------------------------

    def body(self, expected: str) -> "ResponseAssert":
        if self.response.text != expected:
            raise AssertionError(
                "body: expected {!r}, got {!r}".format(expected, self.response.text))
        return self

    def body_contains(self, fragment: str) -> "ResponseAssert":
        if fragment not in self.response.text:
            raise AssertionError(
                "body {!r} does not contain {!r}".format(self.response.text, fragment))
        return self

    def json_equals(self, expected: Any) -> "ResponseAssert":
        actual = self.response.json()
        if actual != expected:
            raise AssertionError("json: expected {!r}, got {!r}".format(expected, actual))
        return self

    def json(self) -> Any:
        """Return the parsed JSON body (a terminal accessor, not a check)."""
        return self.response.json()

    def __repr__(self) -> str:
        return "<ResponseAssert {!r}>".format(self.response)


def assert_that(response) -> ResponseAssert:
    """Wrap ``response`` in a :class:`ResponseAssert` for fluent checks."""
    return ResponseAssert(response)


class CapturingMiddleware:
    """Middleware that records the requests (and resulting statuses) it sees.

    Useful for asserting middleware ordering and short-circuit behaviour: give
    each middleware a distinct ``label`` and inspect the shared :attr:`log`.
    """

    def __init__(self, label: str, log: Optional[List[Tuple[str, str]]] = None) -> None:
        self.label = label
        self.log: List[Tuple[str, str]] = log if log is not None else []

    def __call__(self, request, next_call):
        self.log.append((self.label, "in"))
        response = next_call(request)
        self.log.append((self.label, "out"))
        return response

    def labels_in_order(self) -> List[str]:
        """The labels in the order they were entered (``"in"`` events only)."""
        return [label for label, phase in self.log if phase == "in"]

    def __repr__(self) -> str:
        return "<CapturingMiddleware {!r}>".format(self.label)
