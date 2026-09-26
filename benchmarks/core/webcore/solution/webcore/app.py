"""webcore.app -- the :class:`App` object that ties everything together.

:class:`App` owns a :class:`~webcore.routing.Router`, a list of middlewares, and
a list of mounted sub-applications. Its two jobs are:

*   :meth:`App.dispatch` -- turn a :class:`~webcore.request.Request` into a
    :class:`~webcore.response.Response`, applying routing precedence, method
    handling (200 / 405 / auto-HEAD / auto-OPTIONS), trailing-slash redirects,
    mounting, and the middleware onion.
*   :meth:`App.url_for` -- the reverse of routing: build a URL string for a
    named route from keyword parameters.

Resolution model
----------------
:meth:`App.resolve` returns a flat list of :class:`Match` candidates for a path,
drawing both from the app's own routes and, recursively, from every mounted
sub-app under its (possibly parametric) prefix. Each match carries the merged
parameters, the full outer-to-inner middleware chain, and a precedence key.
``dispatch`` sorts the candidates once and then picks the winner by method.

Because a sub-app is only ever consulted through ``resolve``, middleware is
actually *executed* only at the root ``dispatch`` -- but the chain a match
carries already includes the parent's and then the child's middlewares, so the
onion nests correctly no matter how deep the mounts go.
"""

from .request import Request
from .response import (
    Response,
    text as text_response,
    json_response,
    query_suffix,
)
from .routing import Router, Route, parse_pattern, build_specificity, format_from_tokens
from .converters import get_converter
from .middleware import build_chain


# HTTP methods for which webcore synthesises behaviour.
_SAFE_AUTO_HEAD_FROM = "GET"


class Match:
    """A resolved routing candidate.

    Attributes
    ----------
    methods : frozenset[str]
        Methods the underlying route answers explicitly.
    handler : callable
        ``handler(request, **params)``.
    params : dict
        Merged prefix + route parameters (converted to Python types).
    middleware : list
        Outer-to-inner middlewares to wrap the handler with.
    specificity : tuple[int, ...]
        Full-path precedence key (prefix ranks + route ranks).
    order : tuple[int, ...]
        Registration-order tie-breaker.
    """

    __slots__ = ("methods", "handler", "params", "middleware", "specificity", "order")

    def __init__(self, methods, handler, params, middleware, specificity, order):
        self.methods = methods
        self.handler = handler
        self.params = params
        self.middleware = middleware
        self.specificity = specificity
        self.order = order

    def sort_key(self):
        return (self.specificity, self.order)


class Mount:
    """A sub-application mounted under a (possibly parametric) path prefix.

    The prefix is a route pattern in its own right (``/api/{ver}``). At match
    time the mount consumes the leading segments the prefix describes, captures
    any prefix parameters, and hands the remainder of the path to the sub-app.
    """

    def __init__(self, prefix, subapp, order):
        if not prefix.startswith("/"):
            raise ValueError("mount prefix must start with '/': {!r}".format(prefix))
        # A prefix should not itself carry a trailing slash; normalise it away.
        normalized = prefix.rstrip("/") or "/"
        self.prefix = normalized
        self.subapp = subapp
        self.order = order

        self.tokens, _trailing = parse_pattern(normalized)
        self.specificity = build_specificity(self.tokens)
        self._params = [
            (tok[1], get_converter(tok[2]))
            for tok in self.tokens
            if tok[0] == "param"
        ]
        self._regex = self._compile_prefix_regex()

    def _compile_prefix_regex(self):
        import re

        parts = ["^"]
        for tok in self.tokens:
            if tok[0] == "static":
                parts.append("/" + re.escape(tok[1]))
            else:
                conv = get_converter(tok[2])
                if conv.multi:
                    raise ValueError(
                        "a mount prefix may not use a 'path' converter: {!r}".format(
                            self.prefix
                        )
                    )
                parts.append("/(?P<{}>{})".format(tok[1], conv.regex))
        # Capture the remainder (an optional slash-led tail) for the sub-app.
        parts.append(r"(?P<__tail__>/.*)?$")
        return re.compile("".join(parts))

    def match_prefix(self, path):
        """Return ``(prefix_params, remainder, specificity)`` or ``None``.

        ``remainder`` always starts with ``/`` (it is ``"/"`` when the request
        targets the mount root exactly), so it is a valid path for the sub-app.
        """
        m = self._regex.match(path)
        if m is None:
            return None
        params = {}
        for name, conv in self._params:
            params[name] = conv.to_python(m.group(name))
        tail = m.group("__tail__")
        remainder = tail if tail else "/"
        return params, remainder, self.specificity

    def build_prefix(self, params):
        """Render the concrete prefix path from ``params`` (for url_for)."""
        path, used = format_from_tokens(self.tokens, False, params)
        return path, used


class App:
    """The webcore application.

    Register routes with the :meth:`route` decorator (or :meth:`add_route`),
    add middleware with :meth:`use`, mount sub-apps with :meth:`mount`, and
    drive it all with :meth:`dispatch` (usually via a
    :class:`~webcore.testclient.TestClient`).
    """

    def __init__(self, name=None):
        self.name = name
        self.router = Router()
        self.middlewares = []
        self.mounts = []
        self._order = 0

    # -- registration ----------------------------------------------------

    def _next_order(self):
        order = self._order
        self._order += 1
        return order

    def add_route(self, name, path, handler, methods=("GET",)):
        """Register a route explicitly and return the :class:`Route`."""
        route = Route(name, path, handler, methods, self._next_order())
        self.router.add(route)
        return route

    def route(self, path, methods=("GET",), name=None):
        """Decorator form of :meth:`add_route`.

        ``@app.route("/users/{id:int}")`` registers the decorated function; the
        route name defaults to the function's ``__name__``.
        """

        def decorator(func):
            self.add_route(name or func.__name__, path, func, methods)
            return func

        return decorator

    def use(self, middleware):
        """Append a middleware. Earlier-added middlewares are more *outer*."""
        self.middlewares.append(middleware)
        return middleware

    def mount(self, prefix, subapp):
        """Mount ``subapp`` under ``prefix`` (which may contain parameters)."""
        self.mounts.append(Mount(prefix, subapp, self._next_order()))
        return subapp

    # -- resolution ------------------------------------------------------

    def resolve(self, path):
        """Return every :class:`Match` for ``path`` (own routes + mounts)."""
        matches = []

        for route, params in self.router.match_path(path):
            matches.append(
                Match(
                    route.methods,
                    route.handler,
                    params,
                    list(self.middlewares),
                    route.specificity,
                    (route.order,),
                )
            )

        for mount in self.mounts:
            found = mount.match_prefix(path)
            if found is None:
                continue
            prefix_params, remainder, prefix_spec = found
            for sub in mount.subapp.resolve(remainder):
                merged = dict(prefix_params)
                merged.update(sub.params)
                middleware = list(self.middlewares) + list(sub.middleware)
                matches.append(
                    Match(
                        sub.methods,
                        sub.handler,
                        merged,
                        middleware,
                        prefix_spec + sub.specificity,
                        (mount.order,) + sub.order,
                    )
                )

        return matches

    # -- dispatch --------------------------------------------------------

    def dispatch(self, request):
        """Resolve ``request`` to a :class:`Response`."""
        path = request.path
        matches = self.resolve(path)

        if not matches:
            return self._dispatch_no_match(request, path)

        matches.sort(key=lambda m: m.sort_key())

        allowed = self._allowed_methods(matches)
        method = request.method

        chosen = self._choose(matches, method)
        if chosen is not None:
            return self._run(chosen.middleware, request, self._handler_terminal(chosen))

        # No route on this path answers the method.
        if method == "OPTIONS":
            return self._run(
                list(self.middlewares), request, self._options_terminal(allowed)
            )
        return self._run(
            list(self.middlewares), request, self._method_not_allowed_terminal(allowed)
        )

    def _dispatch_no_match(self, request, path):
        """Handle a path with no matching route: trailing-slash redirect or 404."""
        alt = _toggle_trailing_slash(path)
        if alt is not None and self.resolve(alt):
            return self._run(
                list(self.middlewares),
                request,
                self._redirect_terminal(alt, request.query_string),
            )
        return self._run(list(self.middlewares), request, _not_found_terminal)

    def _choose(self, matches, method):
        """Pick the first match (by precedence) that answers ``method``.

        ``HEAD`` falls back to a ``GET`` route (clause 10). Returns ``None`` if
        no candidate answers the method.
        """
        wanted = [method]
        if method == "HEAD":
            wanted = ["HEAD", _SAFE_AUTO_HEAD_FROM]
        for cand in wanted:
            for match in matches:
                if cand in match.methods:
                    return match
        return None

    def _allowed_methods(self, matches):
        """Union of methods across all path matches, plus auto HEAD/OPTIONS."""
        allowed = set()
        for match in matches:
            allowed |= set(match.methods)
        if _SAFE_AUTO_HEAD_FROM in allowed:
            allowed.add("HEAD")
        allowed.add("OPTIONS")
        return allowed

    # -- terminals (the innermost callable of a middleware chain) --------

    def _handler_terminal(self, match):
        def terminal(request):
            if request.method == "HEAD":
                get_request = request.copy(method=_SAFE_AUTO_HEAD_FROM)
                resp = _coerce(match.handler(get_request, **match.params))
                # Same headers, empty body (clause 10).
                return Response(resp.status, resp.headers.copy(), b"")
            return _coerce(match.handler(request, **match.params))

        return terminal

    def _options_terminal(self, allowed):
        def terminal(request):
            resp = Response(204, None, b"")
            resp.headers["Allow"] = _allow_header(allowed)
            return resp

        return terminal

    def _method_not_allowed_terminal(self, allowed):
        def terminal(request):
            resp = Response(405, None, "Method Not Allowed")
            resp.headers["Allow"] = _allow_header(allowed)
            resp.headers.setdefault("Content-Type", "text/plain; charset=utf-8")
            return resp

        return terminal

    def _redirect_terminal(self, location, query_string):
        target = location
        if query_string:
            target = location + "?" + query_string

        def terminal(request):
            resp = Response(308, None, b"")
            resp.headers["Location"] = target
            return resp

        return terminal

    def _run(self, middlewares, request, terminal):
        chain = build_chain(middlewares, terminal)
        return _coerce(chain(request))

    # -- reverse routing -------------------------------------------------

    def url_for(self, name, **params):
        """Build the URL string for the named route.

        Fills the route's (and any enclosing mount prefixes') parameters from
        ``params``, formatting each through its converter. Any leftover keyword
        arguments become a sorted, URL-encoded query string appended after
        ``?``. A missing or invalid parameter raises ``KeyError`` / ``ValueError``
        (clause 9).
        """
        found = self._find_named(name)
        if found is None:
            raise KeyError("no route named {!r}".format(name))
        prefix_tokens, route = found

        remaining = dict(params)
        prefix_path, prefix_used = format_from_tokens(prefix_tokens, False, remaining)
        for key in prefix_used:
            remaining.pop(key, None)

        route_path, route_used = route.build(remaining)
        for key in route_used:
            remaining.pop(key, None)

        # Join prefix and route paths, avoiding a doubled or missing slash.
        if prefix_path == "/":
            full = route_path
        elif route_path == "/":
            full = prefix_path
        else:
            full = prefix_path + route_path

        return full + query_suffix(remaining)

    def _find_named(self, name):
        """Return ``(prefix_tokens, route)`` for a named route, searching mounts.

        ``prefix_tokens`` is the concatenation of the token lists of every mount
        prefix on the way down to the route (empty for a top-level route).
        """
        route = self.router.by_name(name)
        if route is not None:
            return [], route
        for mount in self.mounts:
            sub = mount.subapp._find_named(name)
            if sub is not None:
                sub_prefix_tokens, sub_route = sub
                return list(mount.tokens) + sub_prefix_tokens, sub_route
        return None


# -- module-level terminals / helpers ------------------------------------


def _not_found_terminal(request):
    resp = Response(404, None, "Not Found")
    resp.headers.setdefault("Content-Type", "text/plain; charset=utf-8")
    return resp


def _allow_header(methods):
    """Sorted, comma-separated Allow header value (clauses 8 & 10)."""
    return ", ".join(sorted(methods))


def _toggle_trailing_slash(path):
    """Return the other trailing-slash form of ``path``, or ``None``.

    ``/items`` -> ``/items/`` and ``/items/`` -> ``/items``. The root ``/`` has
    no alternate form, and neither does an empty result.
    """
    if path == "/" or path == "":
        return None
    if path.endswith("/"):
        alt = path[:-1]
    else:
        alt = path + "/"
    return alt or None


def _coerce(result):
    """Coerce a handler/middleware return value into a :class:`Response`.

    Accepts an existing ``Response`` (passed through), a ``str`` (text/plain), a
    ``dict``/``list`` (JSON), ``bytes`` (raw body), or a ``(body, status)`` /
    ``(body, status, headers)`` tuple.
    """
    if isinstance(result, Response):
        return result
    if isinstance(result, str):
        return text_response(result)
    if isinstance(result, (dict, list)):
        return json_response(result)
    if isinstance(result, bytes):
        return Response(200, None, result)
    if isinstance(result, tuple):
        if len(result) == 2:
            body, status = result
            resp = _coerce(body)
            resp.status = int(status)
            return resp
        if len(result) == 3:
            body, status, headers = result
            resp = _coerce(body)
            resp.status = int(status)
            for key, value in (headers.items() if hasattr(headers, "items") else headers):
                resp.headers[key] = value
            return resp
    raise TypeError("cannot coerce {!r} to a Response".format(type(result).__name__))
