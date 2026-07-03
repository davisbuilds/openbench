"""webcore.response -- the :class:`Response` object, header handling, helpers.

:class:`Headers` is a small case-insensitive, order-preserving mapping. HTTP
header names are case-insensitive on the wire, so ``resp.headers["allow"]`` and
``resp.headers["Allow"]`` return the same value while the original casing is
kept for display.

:class:`Response` carries a status code, headers, and a body (``str`` or
``bytes``). The convenience constructors :func:`text`, :func:`json_response`,
and :func:`redirect` set a sensible ``Content-Type`` / ``Location`` for you.
"""

import json as _json
from urllib.parse import urlencode

from . import status as _status


class Headers:
    """Case-insensitive, order-preserving header map."""

    def __init__(self, initial=None):
        # maps lower-case name -> (original_name, value)
        self._store = {}
        if initial:
            source = initial.items() if hasattr(initial, "items") else initial
            for key, value in source:
                self[key] = value

    def __setitem__(self, key, value):
        self._store[key.lower()] = (key, value)

    def __getitem__(self, key):
        return self._store[key.lower()][1]

    def __delitem__(self, key):
        del self._store[key.lower()]

    def __contains__(self, key):
        return key.lower() in self._store

    def __iter__(self):
        return (orig for orig, _ in self._store.values())

    def __len__(self):
        return len(self._store)

    def get(self, key, default=None):
        item = self._store.get(key.lower())
        return item[1] if item is not None else default

    def setdefault(self, key, default):
        if key.lower() not in self._store:
            self[key] = default
        return self[key]

    def items(self):
        return [(orig, value) for orig, value in self._store.values()]

    def keys(self):
        return [orig for orig, _ in self._store.values()]

    def values(self):
        return [value for _, value in self._store.values()]

    def copy(self):
        new = Headers()
        new._store = dict(self._store)
        return new

    def __eq__(self, other):
        if isinstance(other, Headers):
            other_norm = {k: v[1] for k, v in other._store.items()}
        elif hasattr(other, "items"):
            other_norm = {k.lower(): v for k, v in other.items()}
        else:
            return NotImplemented
        mine = {k: v[1] for k, v in self._store.items()}
        return mine == other_norm

    def __repr__(self):
        return "Headers({!r})".format(self.items())


class Response:
    """An HTTP response.

    Parameters
    ----------
    status : int
        Status code (default 200).
    headers : mapping | list, optional
        Initial headers.
    body : str | bytes, optional
        Response body. ``str`` bodies are UTF-8 encoded by :meth:`body_bytes`.
    """

    def __init__(self, status=200, headers=None, body=b""):
        self.status = int(status)
        self.headers = headers if isinstance(headers, Headers) else Headers(headers)
        self.body = body

    @property
    def reason(self):
        return _status.reason_phrase(self.status)

    @property
    def text(self):
        """The body decoded to ``str`` (bytes are treated as UTF-8)."""
        if isinstance(self.body, bytes):
            return self.body.decode("utf-8")
        return self.body if self.body is not None else ""

    def body_bytes(self):
        """The body as ``bytes``."""
        if isinstance(self.body, bytes):
            return self.body
        if self.body is None:
            return b""
        return str(self.body).encode("utf-8")

    def json(self):
        """Parse the body as JSON and return the resulting object."""
        return _json.loads(self.text)

    def get_header(self, name, default=None):
        return self.headers.get(name, default)

    def with_body(self, body):
        """Return a copy of this response with a different body (same headers)."""
        return Response(self.status, self.headers.copy(), body)

    def set_cookie(self, name, value, path="/", max_age=None, http_only=False,
                   secure=False, same_site=None):
        """Append a ``Set-Cookie`` header for ``name=value``.

        Only the attributes webcore needs are supported; the header is built in
        the conventional ``name=value; Attr=...`` order.
        """
        parts = ["{}={}".format(name, value)]
        if path:
            parts.append("Path=" + path)
        if max_age is not None:
            parts.append("Max-Age=" + str(int(max_age)))
        if same_site:
            parts.append("SameSite=" + same_site)
        if secure:
            parts.append("Secure")
        if http_only:
            parts.append("HttpOnly")
        cookie = "; ".join(parts)
        # Multiple cookies coexist; join with a newline-free comma is invalid, so
        # keep the most recent under the header (sufficient for in-process use).
        self.headers["Set-Cookie"] = cookie
        return self

    def __repr__(self):
        return "<Response {} {} bytes>".format(self.status, len(self.body_bytes()))


def text(body, status=200, headers=None):
    """Build a ``text/plain; charset=utf-8`` response."""
    resp = Response(status, headers, body)
    resp.headers.setdefault("Content-Type", "text/plain; charset=utf-8")
    return resp


def json_response(data, status=200, headers=None):
    """Build an ``application/json`` response from a JSON-serialisable object."""
    body = _json.dumps(data, sort_keys=True, separators=(",", ":"))
    resp = Response(status, headers, body)
    resp.headers.setdefault("Content-Type", "application/json")
    return resp


def html(body, status=200, headers=None):
    """Build a ``text/html; charset=utf-8`` response."""
    resp = Response(status, headers, body)
    resp.headers.setdefault("Content-Type", "text/html; charset=utf-8")
    return resp


def no_content(headers=None):
    """Build a bodyless ``204 No Content`` response."""
    return Response(204, headers, b"")


def redirect(location, status=302, headers=None):
    """Build a redirect response with the given ``Location``."""
    resp = Response(status, headers, b"")
    resp.headers["Location"] = location
    return resp


def query_suffix(params):
    """Render ``params`` as a sorted, URL-encoded ``?a=1&b=2`` suffix.

    Returns an empty string when ``params`` is empty. Keys are sorted so the
    output is deterministic (clause 9).
    """
    if not params:
        return ""
    items = sorted((str(k), str(v)) for k, v in params.items())
    return "?" + urlencode(items)
