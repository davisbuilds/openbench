"""webcore.request -- the immutable-ish :class:`Request` value object.

A request is created from an HTTP method and a target path. If the path carries
a query string (``/search?q=hi&page=2``) the request splits it off, keeps the
raw string in :attr:`Request.query_string`, and parses it into
:attr:`Request.query` (a plain ``dict`` where a repeated key keeps its *last*
value), :attr:`Request.query_list` (the full ordered list of pairs), and
:attr:`Request.query_multi` (a :class:`~webcore.datastructures.MultiDict`).

Header lookups are case-insensitive: internally headers are stored lower-cased,
and :meth:`Request.header` restores that convenience. Convenience accessors are
provided for cookies (:attr:`Request.cookies`), the parsed content type
(:attr:`Request.content_type`), simple form/JSON bodies (:meth:`Request.form`,
:meth:`Request.json`), and ``Accept`` negotiation (:meth:`Request.accept_mimetypes`,
:meth:`Request.accepts`).
"""

import json as _json
from urllib.parse import parse_qsl

from .datastructures import MultiDict


def parse_query(query_string):
    """Parse a raw query string into ``(dict, list_of_pairs)``.

    ``a=1&b=2&a=3`` -> ``({"a": "3", "b": "2"}, [("a","1"),("b","2"),("a","3")])``.
    Percent escapes and ``+`` are decoded (``a=hi%20there`` -> ``"hi there"``),
    and a bare key (``?flag``) yields an empty-string value.
    """
    pairs = parse_qsl(query_string, keep_blank_values=True)
    as_dict = {}
    for key, value in pairs:
        as_dict[key] = value
    return as_dict, pairs


def parse_cookie_header(value):
    """Parse a ``Cookie:`` header value into a plain ``dict``.

    ``"a=1; b=2"`` -> ``{"a": "1", "b": "2"}``. Malformed segments (no ``=``) are
    skipped. Later duplicates win.
    """
    cookies = {}
    if not value:
        return cookies
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, val = chunk.partition("=")
        cookies[name.strip()] = val.strip()
    return cookies


def parse_content_type(value):
    """Split a Content-Type header into ``(mimetype, params_dict)``.

    ``"text/html; charset=utf-8"`` -> ``("text/html", {"charset": "utf-8"})``.
    """
    if not value:
        return "", {}
    parts = value.split(";")
    mimetype = parts[0].strip().lower()
    params = {}
    for part in parts[1:]:
        part = part.strip()
        if "=" in part:
            key, _, val = part.partition("=")
            params[key.strip().lower()] = val.strip().strip('"')
    return mimetype, params


def parse_accept(value):
    """Parse an ``Accept`` header into a list of ``(mimetype, quality)`` pairs.

    Sorted by descending quality (``q=`` weight, default ``1.0``); ties keep the
    header's original order.
    """
    if not value:
        return []
    items = []
    for index, part in enumerate(value.split(",")):
        part = part.strip()
        if not part:
            continue
        pieces = part.split(";")
        mimetype = pieces[0].strip().lower()
        quality = 1.0
        for piece in pieces[1:]:
            piece = piece.strip()
            if piece.startswith("q="):
                try:
                    quality = float(piece[2:])
                except ValueError:
                    quality = 0.0
        items.append((mimetype, quality, index))
    items.sort(key=lambda t: (-t[1], t[2]))
    return [(mimetype, quality) for mimetype, quality, _ in items]


class Request:
    """An incoming request.

    Parameters
    ----------
    method : str
        HTTP method; stored upper-cased.
    path : str
        Target, optionally including ``?query``. The query is split off and the
        stored :attr:`path` never contains it.
    headers : mapping, optional
        Header name/value pairs; names are compared case-insensitively.
    body : bytes | str, optional
        Request body; opaque to routing.
    """

    def __init__(self, method, path, headers=None, body=b""):
        self.method = method.upper()

        if "?" in path:
            path, query_string = path.split("?", 1)
        else:
            query_string = ""
        self.path = path or "/"
        self.query_string = query_string
        self.query, self.query_list = parse_query(query_string)
        self.query_multi = MultiDict(self.query_list)

        self._headers = {}
        if headers:
            for key, value in dict(headers).items():
                self._headers[key.lower()] = value
        self.body = body

    # -- headers ---------------------------------------------------------

    def header(self, name, default=None):
        """Case-insensitive header lookup."""
        return self._headers.get(name.lower(), default)

    @property
    def headers(self):
        """The (lower-cased) header mapping."""
        return dict(self._headers)

    # -- cookies ---------------------------------------------------------

    @property
    def cookies(self):
        """Parsed ``Cookie:`` header as a ``dict`` (empty if none)."""
        return parse_cookie_header(self.header("cookie", ""))

    # -- content type / body ---------------------------------------------

    @property
    def content_type(self):
        """The bare mimetype from the ``Content-Type`` header (lower-cased)."""
        mimetype, _params = parse_content_type(self.header("content-type", ""))
        return mimetype

    @property
    def charset(self):
        """The ``charset`` parameter of the Content-Type, or ``None``."""
        _mimetype, params = parse_content_type(self.header("content-type", ""))
        return params.get("charset")

    @property
    def is_json(self):
        """True if the request declares a JSON content type."""
        return self.content_type == "application/json"

    def _body_text(self):
        if isinstance(self.body, bytes):
            return self.body.decode(self.charset or "utf-8")
        return self.body if self.body is not None else ""

    def json(self):
        """Parse the request body as JSON."""
        return _json.loads(self._body_text())

    def form(self):
        """Parse a ``application/x-www-form-urlencoded`` body into a MultiDict."""
        return MultiDict(parse_qsl(self._body_text(), keep_blank_values=True))

    # -- content negotiation --------------------------------------------

    def accept_mimetypes(self):
        """The parsed ``Accept`` header as ``[(mimetype, quality), ...]``."""
        return parse_accept(self.header("accept", ""))

    def accepts(self, mimetype):
        """True if the client accepts ``mimetype`` (``*/*`` counts)."""
        offered = mimetype.lower()
        family = offered.split("/", 1)[0] + "/*"
        for accepted, quality in self.accept_mimetypes():
            if quality <= 0:
                continue
            if accepted in (offered, family, "*/*"):
                return True
        return False

    # -- misc ------------------------------------------------------------

    @property
    def full_path(self):
        """The path with its query string re-attached (if any)."""
        if self.query_string:
            return self.path + "?" + self.query_string
        return self.path

    def copy(self, method=None, path=None):
        """Return a shallow copy, optionally overriding method or path.

        Used when auto-answering ``HEAD`` by invoking the ``GET`` handler with
        the method swapped (clause 10).
        """
        target = path if path is not None else self.full_path
        clone = Request(
            method if method is not None else self.method,
            target,
            self._headers,
            self.body,
        )
        return clone

    def __repr__(self):
        return "<Request {} {}>".format(self.method, self.full_path)
