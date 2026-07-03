"""webcore.routing -- pattern parsing, :class:`Route`, and :class:`Router`.

A route pattern is a ``/``-delimited path. Each segment is either static text
or a ``{name}`` placeholder that captures exactly one non-empty segment and is
delivered to the handler as a string. Examples::

    /users
    /users/{name}
    /articles/{slug}

Routes are matched in the order they were registered: :meth:`Router.match_path`
walks the list from first to last and returns everything whose pattern matches
the request path, preserving that registration order.
"""

import re

from .converters import get_converter


_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RouteError(ValueError):
    """Raised for a malformed route pattern."""


def _param_name(body):
    """Return the parameter name from a placeholder body.

    A bare ``{name}`` yields ``"name"``. Anything after a colon is ignored --
    the framework only supports plain string parameters right now.
    """
    name = body.split(":", 1)[0].strip()
    if not _NAME_RE.match(name):
        raise RouteError("invalid parameter name {!r}".format(name))
    return name


def parse_pattern(pattern):
    """Split a pattern into a list of ``("static", text)`` / ``("param", name)``.

    A trailing slash is stripped so ``/dir/`` and ``/dir`` register the same
    route. The root pattern ``"/"`` yields an empty token list.
    """
    if not pattern.startswith("/"):
        raise RouteError("route pattern must start with '/': {!r}".format(pattern))
    if pattern == "/":
        return []

    body = pattern
    if body.endswith("/"):
        body = body[:-1]

    tokens = []
    for seg in body.split("/")[1:]:
        if seg == "":
            raise RouteError("empty path segment in {!r}".format(pattern))
        if seg.startswith("{") and seg.endswith("}"):
            tokens.append(("param", _param_name(seg[1:-1])))
        elif "{" in seg or "}" in seg:
            raise RouteError("a placeholder must be a whole segment: {!r}".format(seg))
        else:
            tokens.append(("static", seg))
    return tokens


class Route:
    """A single registered route: a compiled pattern plus its handler."""

    def __init__(self, name, pattern, handler, methods=("GET",), order=0):
        self.name = name
        self.pattern = pattern
        self.handler = handler
        self.methods = frozenset(m.upper() for m in methods)
        self.order = order

        self.tokens = parse_pattern(pattern)
        self._converter = get_converter()
        self._regex = re.compile(self._compile_regex())
        self._param_names = [tok[1] for tok in self.tokens if tok[0] == "param"]

    def _compile_regex(self):
        parts = ["^"]
        for tok in self.tokens:
            if tok[0] == "static":
                parts.append("/" + re.escape(tok[1]))
            else:
                parts.append("/(?P<{}>{})".format(tok[1], self._converter.regex))
        if not self.tokens:
            parts.append("/")
        parts.append("$")
        return "".join(parts)

    def match(self, path):
        """Return a params dict if ``path`` matches, else ``None``."""
        m = self._regex.match(path)
        if m is None:
            return None
        return {name: self._converter.to_python(m.group(name)) for name in self._param_names}

    def build(self, params):
        """Render this route's path, substituting ``{name}`` placeholders.

        Missing placeholders are left as their literal ``{name}`` text.
        """
        parts = []
        used = set()
        for tok in self.tokens:
            if tok[0] == "static":
                parts.append(tok[1])
            else:
                name = tok[1]
                if name in params:
                    parts.append(self._converter.to_url(params[name]))
                    used.add(name)
                else:
                    parts.append("{" + name + "}")
        path = "/" + "/".join(parts) if parts else "/"
        return path, used

    def __repr__(self):
        return "<Route {!r} {} methods={}>".format(
            self.pattern, self.name, sorted(self.methods)
        )


class Router:
    """An ordered collection of routes, matched in registration order."""

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
        """Return ``[(route, params), ...]`` for all matches, in registration order."""
        matched = []
        for route in self.routes:
            params = route.match(path)
            if params is not None:
                matched.append((route, params))
        return matched

    def by_name(self, name):
        for route in self.routes:
            if route.name == name:
                return route
        return None

    def __len__(self):
        return len(self.routes)

    def __iter__(self):
        return iter(self.routes)
