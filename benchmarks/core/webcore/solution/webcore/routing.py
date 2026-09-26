"""webcore.routing -- pattern parsing, :class:`Route`, and :class:`Router`.

A route pattern is a ``/``-delimited path where each segment is either static
text or a single ``{name}`` / ``{name:converter}`` placeholder occupying the
whole segment. Examples::

    /users
    /users/{id:int}
    /articles/{slug:slug}
    /files/{rest:path}
    /users/{id:int}/comments/{cid:int}

Mixing static text and a placeholder inside one segment (``/file-{id}.txt``) is
intentionally unsupported -- a placeholder is always a full segment. This keeps
the compiled regex simple and the precedence rules unambiguous.

Precedence (clause 3)
---------------------
Every route carries a :attr:`Route.specificity` tuple, one rank per segment:

*   ``0`` -- a static segment (most specific),
*   ``1`` -- a single-segment converter (``str``/``int``/``slug``),
*   ``2`` -- the multi-segment ``path`` converter (least specific).

Sorting candidate routes by ``(specificity, registration_order)`` ascending
means: a static segment beats a dynamic one at the same position, more-static
routes are tried before more-dynamic ones, ``path`` routes come last, and exact
ties fall back to the order the routes were registered in.
"""

import re

from .converters import get_converter, DEFAULT_CONVERTER


# Ranks used to build a route's specificity tuple.
_RANK_STATIC = 0
_RANK_DYNAMIC = 1
_RANK_PATH = 2

# A placeholder body is ``name`` or ``name:converter``; the name must be a
# valid Python identifier so it can also be a regex group name and a handler
# keyword argument.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RouteError(ValueError):
    """Raised for a malformed route pattern."""


def parse_param(body):
    """Parse a placeholder body (the text between ``{`` and ``}``).

    ``"id"`` -> ``("id", "str")``; ``"id:int"`` -> ``("id", "int")``.
    Raises :class:`RouteError` on an invalid name or an empty converter.
    """
    if ":" in body:
        name, _, conv = body.partition(":")
        conv = conv.strip()
        if conv == "":
            raise RouteError("empty converter in placeholder {{{}}}".format(body))
    else:
        name, conv = body, DEFAULT_CONVERTER
    name = name.strip()
    if not _NAME_RE.match(name):
        raise RouteError("invalid parameter name {!r}".format(name))
    return name, conv


def parse_pattern(pattern):
    """Split a route pattern into ``(tokens, trailing_slash)``.

    ``tokens`` is a list where each entry is either ``("static", text)`` or
    ``("param", name, converter_name)``. ``trailing_slash`` records whether the
    pattern was written with a trailing ``/`` (canonical form, clause 7).

    The root pattern ``"/"`` yields ``([], False)``.
    """
    if not pattern.startswith("/"):
        raise RouteError("route pattern must start with '/': {!r}".format(pattern))

    if pattern == "/":
        return [], False

    trailing = False
    body = pattern
    if body.endswith("/"):
        trailing = True
        body = body[:-1]

    tokens = []
    seen_names = set()
    segments = body.split("/")[1:]  # drop the leading empty piece
    for seg in segments:
        if seg == "":
            raise RouteError("empty path segment in {!r} (double slash?)".format(pattern))
        if seg.startswith("{") and seg.endswith("}"):
            name, conv = parse_param(seg[1:-1])
            if name in seen_names:
                raise RouteError("duplicate parameter name {!r} in {!r}".format(name, pattern))
            seen_names.add(name)
            tokens.append(("param", name, conv))
        elif "{" in seg or "}" in seg:
            raise RouteError(
                "a placeholder must be a whole segment: {!r}".format(seg)
            )
        else:
            tokens.append(("static", seg))
    return tokens, trailing


def _segment_regex(name, converter):
    """Return the regex fragment for one dynamic segment (with its ``/``)."""
    return "/(?P<{}>{})".format(name, converter.regex)


def build_specificity(tokens):
    """Return the specificity rank tuple for a token list."""
    ranks = []
    for tok in tokens:
        if tok[0] == "static":
            ranks.append(_RANK_STATIC)
        else:
            conv = get_converter(tok[2])
            ranks.append(_RANK_PATH if conv.multi else _RANK_DYNAMIC)
    return tuple(ranks)


def format_from_tokens(tokens, trailing, params):
    """Render a ``(path, used_keys)`` pair from tokens and a params mapping.

    Used by :func:`webcore.app.App.url_for`. Each dynamic token pops its value
    from ``params`` and renders it via the converter's ``to_url`` (which
    validates it). A missing parameter raises :class:`KeyError`; an invalid one
    raises :class:`ValueError` (from the converter). ``used_keys`` is the set of
    parameter names consumed, so the caller can turn the leftovers into a query
    string.
    """
    parts = []
    used = set()
    for tok in tokens:
        if tok[0] == "static":
            parts.append(tok[1])
        else:
            name, conv_name = tok[1], tok[2]
            if name not in params:
                raise KeyError(
                    "missing value for URL parameter {!r}".format(name)
                )
            conv = get_converter(conv_name)
            parts.append(conv.to_url(params[name]))
            used.add(name)
    path = "/" + "/".join(parts) if parts else "/"
    if trailing and path != "/":
        path = path + "/"
    return path, used


class Route:
    """A single registered route: a compiled pattern plus its handler.

    Attributes
    ----------
    name : str
        Reverse-lookup name for :func:`url_for`.
    pattern : str
        The original pattern string.
    handler : callable
        ``handler(request, **params) -> response-ish``.
    methods : frozenset[str]
        Upper-cased HTTP methods this route answers.
    tokens, trailing : parsed pattern (see :func:`parse_pattern`).
    specificity : tuple[int, ...]
        Precedence key (see module docstring).
    order : int
        Registration order, used to break specificity ties.
    """

    def __init__(self, name, pattern, handler, methods=("GET",), order=0):
        self.name = name
        self.pattern = pattern
        self.handler = handler
        self.methods = frozenset(m.upper() for m in methods)
        self.order = order

        self.tokens, self.trailing = parse_pattern(pattern)
        self._validate_path_converter()
        self.specificity = build_specificity(self.tokens)
        self._regex = re.compile(self._compile_regex())
        # Cache the (name, converter) pairs for fast to_python conversion.
        self._params = [
            (tok[1], get_converter(tok[2]))
            for tok in self.tokens
            if tok[0] == "param"
        ]

    def _validate_path_converter(self):
        """A ``path`` converter is only legal as the final segment (clause 1)."""
        for index, tok in enumerate(self.tokens):
            if tok[0] == "param" and get_converter(tok[2]).multi:
                if index != len(self.tokens) - 1:
                    raise RouteError(
                        "a 'path' converter may only appear as the last "
                        "segment: {!r}".format(self.pattern)
                    )

    def _compile_regex(self):
        parts = ["^"]
        for tok in self.tokens:
            if tok[0] == "static":
                parts.append("/" + re.escape(tok[1]))
            else:
                parts.append(_segment_regex(tok[1], get_converter(tok[2])))
        if not self.tokens:
            parts.append("/")  # root pattern matches exactly "/"
        elif self.trailing:
            parts.append("/")
        parts.append("$")
        return "".join(parts)

    def match(self, path):
        """Return a params dict if ``path`` matches, else ``None``.

        The dict values are already run through each converter's ``to_python``,
        so an ``int`` parameter arrives as an ``int`` (clause 4). A segment that
        fails its converter simply makes the regex not match, so this returns
        ``None`` and the router falls through (clause 2).
        """
        m = self._regex.match(path)
        if m is None:
            return None
        params = {}
        for name, conv in self._params:
            params[name] = conv.to_python(m.group(name))
        return params

    def build(self, params):
        """Reverse the route: render ``(path, used_keys)`` from ``params``."""
        return format_from_tokens(self.tokens, self.trailing, params)

    def __repr__(self):
        return "<Route {!r} {} methods={}>".format(
            self.pattern, self.name, sorted(self.methods)
        )


class Router:
    """An ordered collection of routes with path-first matching.

    :meth:`match_path` returns *every* route whose pattern matches a path,
    ignoring the HTTP method, sorted by precedence. The application layer then
    decides method handling (200 / 405 / auto-OPTIONS / HEAD) from that list --
    keeping the "does the path exist?" question separate from "is the method
    allowed?" is what lets webcore return 405 instead of 404 (clause 8).
    """

    def __init__(self):
        self.routes = []

    def add(self, route):
        self.routes.append(route)
        return route

    def add_route(self, name, pattern, handler, methods=("GET",), order=None):
        if order is None:
            order = len(self.routes)
        return self.add(Route(name, pattern, handler, methods, order))

    def match_path(self, path):
        """Return ``[(route, params), ...]`` for all path matches, by precedence."""
        matched = []
        for route in self.routes:
            params = route.match(path)
            if params is not None:
                matched.append((route, params))
        matched.sort(key=lambda rp: (rp[0].specificity, rp[0].order))
        return matched

    def by_name(self, name):
        """Return the first route registered under ``name``, or ``None``."""
        for route in self.routes:
            if route.name == name:
                return route
        return None

    def __len__(self):
        return len(self.routes)

    def __iter__(self):
        return iter(self.routes)
