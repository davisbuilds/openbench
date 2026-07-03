"""webcore.app -- the :class:`App` object that ties everything together.

:class:`App` owns a :class:`~webcore.routing.Router` and a list of middlewares.
Its jobs are:

*   :meth:`App.dispatch` -- turn a :class:`~webcore.request.Request` into a
    :class:`~webcore.response.Response`, applying routing and the middleware
    onion, and returning 404 when no route matches.
*   :meth:`App.url_for` -- build a URL string for a named route by substituting
    its ``{name}`` placeholders.
"""

from .response import (
    Response,
    text as text_response,
    json_response,
)
from .routing import Router, Route
from .middleware import build_chain


class App:
    """The webcore application.

    Register routes with the :meth:`route` decorator (or :meth:`add_route`),
    add middleware with :meth:`use`, and drive it all with :meth:`dispatch`
    (usually via a :class:`~webcore.testclient.TestClient`).
    """

    def __init__(self, name=None):
        self.name = name
        self.router = Router()
        self.middlewares = []
        self._order = 0

    # -- registration ----------------------------------------------------

    def _next_order(self):
        order = self._order
        self._order += 1
        return order

    def add_route(self, name, path, handler, methods=("GET",)):
        route = Route(name, path, handler, methods, self._next_order())
        self.router.add(route)
        return route

    def route(self, path, methods=("GET",), name=None):
        def decorator(func):
            self.add_route(name or func.__name__, path, func, methods)
            return func

        return decorator

    def use(self, middleware):
        """Append a middleware. Earlier-added middlewares are more *outer*."""
        self.middlewares.append(middleware)
        return middleware

    def mount(self, prefix, subapp):
        """Mounting a sub-application is not supported yet."""
        raise NotImplementedError("sub-application mounting is not implemented")

    # -- dispatch --------------------------------------------------------

    def dispatch(self, request):
        """Resolve ``request`` to a :class:`Response`."""
        chosen = None
        for route, params in self.router.match_path(request.path):
            if request.method in route.methods:
                chosen = (route, params)
                break

        if chosen is None:
            terminal = _not_found_terminal
        else:
            route, params = chosen

            def terminal(req, _route=route, _params=params):
                return _coerce(_route.handler(req, **_params))

        chain = build_chain(list(self.middlewares), terminal)
        return _coerce(chain(request))

    # -- reverse routing -------------------------------------------------

    def url_for(self, name, **params):
        """Build the URL string for the named route by filling its placeholders."""
        route = self.router.by_name(name)
        if route is None:
            raise KeyError("no route named {!r}".format(name))
        path, _used = route.build(params)
        return path


def _not_found_terminal(request):
    resp = Response(404, None, "Not Found")
    resp.headers.setdefault("Content-Type", "text/plain; charset=utf-8")
    return resp


def _coerce(result):
    """Coerce a handler/middleware return value into a :class:`Response`."""
    if isinstance(result, Response):
        return result
    if isinstance(result, str):
        return text_response(result)
    if isinstance(result, (dict, list)):
        return json_response(result)
    if isinstance(result, bytes):
        return Response(200, None, result)
    raise TypeError("cannot coerce {!r} to a Response".format(type(result).__name__))
