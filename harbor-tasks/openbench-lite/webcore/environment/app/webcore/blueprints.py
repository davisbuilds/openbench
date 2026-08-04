"""webcore.blueprints -- group related routes for deferred registration.

A :class:`Blueprint` is a *recording* of routes and middleware that is not bound
to any application yet. You declare a cohesive slice of your app (say, everything
under ``/admin``) against a blueprint, then :meth:`Blueprint.register` it onto a
real :class:`~webcore.app.App` -- optionally under an extra URL prefix and name
namespace. This keeps large applications navigable without a hard dependency on
an app instance at import time.

Blueprints do not re-implement routing; they defer to the app's own
``add_route`` / ``use`` / ``mount`` so registered routes behave identically to
directly-declared ones (precedence, converters, ``url_for`` and all).

Example
-------
::

    admin = Blueprint("admin", url_prefix="/admin")

    @admin.route("/users/{id:int}", name="user")
    def user(request, id):
        return {"id": id}

    app = App()
    admin.register(app)          # route name becomes "admin.user"
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Tuple

from .urls import join_paths

__all__ = ["Blueprint", "DeferredRoute"]


class DeferredRoute:
    """A recorded route: its path, handler, methods and (local) name."""

    __slots__ = ("path", "handler", "methods", "name")

    def __init__(self, path: str, handler: Callable, methods: Sequence[str],
                 name: Optional[str]) -> None:
        self.path = path
        self.handler = handler
        self.methods = tuple(methods)
        self.name = name or handler.__name__

    def __repr__(self) -> str:
        return "<DeferredRoute {!r} {}>".format(self.path, list(self.methods))


class Blueprint:
    """A named, unbound collection of routes and middleware.

    Parameters
    ----------
    name:
        Namespace prefix for route names (``"admin"`` -> ``"admin.user"``).
    url_prefix:
        Path prepended to every route in the blueprint at registration time.
    """

    def __init__(self, name: str, url_prefix: str = "") -> None:
        self.name = name
        self.url_prefix = url_prefix
        self._routes: List[DeferredRoute] = []
        self._middlewares: List[Callable] = []
        self._children: List[Tuple["Blueprint", str]] = []

    # -- declaration -----------------------------------------------------

    def add_route(self, path: str, handler: Callable,
                  methods: Sequence[str] = ("GET",),
                  name: Optional[str] = None) -> DeferredRoute:
        """Record a route to be created when this blueprint is registered."""
        route = DeferredRoute(path, handler, methods, name)
        self._routes.append(route)
        return route

    def route(self, path: str, methods: Sequence[str] = ("GET",),
              name: Optional[str] = None) -> Callable:
        """Decorator form of :meth:`add_route`."""
        def decorator(func: Callable) -> Callable:
            self.add_route(path, func, methods, name)
            return func
        return decorator

    def get(self, path: str, name: Optional[str] = None) -> Callable:
        return self.route(path, methods=("GET",), name=name)

    def post(self, path: str, name: Optional[str] = None) -> Callable:
        return self.route(path, methods=("POST",), name=name)

    def put(self, path: str, name: Optional[str] = None) -> Callable:
        return self.route(path, methods=("PUT",), name=name)

    def delete(self, path: str, name: Optional[str] = None) -> Callable:
        return self.route(path, methods=("DELETE",), name=name)

    def patch(self, path: str, name: Optional[str] = None) -> Callable:
        return self.route(path, methods=("PATCH",), name=name)

    def use(self, middleware: Callable) -> Callable:
        """Record a middleware to be applied when registered onto an app."""
        self._middlewares.append(middleware)
        return middleware

    def include(self, child: "Blueprint", url_prefix: str = "") -> None:
        """Nest another blueprint under this one (composed at register time)."""
        self._children.append((child, url_prefix))

    # -- registration ----------------------------------------------------

    def iter_routes(self, parent_prefix: str = "",
                    parent_name: str = "") -> List[Tuple[str, Callable, Tuple[str, ...], str]]:
        """Flatten this blueprint (and children) into concrete route tuples.

        Returns ``(full_path, handler, methods, qualified_name)`` entries with
        prefixes joined and names namespaced -- the exact arguments an app's
        ``add_route`` expects.
        """
        prefix = join_paths(parent_prefix, self.url_prefix) if self.url_prefix else parent_prefix
        namespace = "{}.{}".format(parent_name, self.name) if parent_name else self.name

        resolved: List[Tuple[str, Callable, Tuple[str, ...], str]] = []
        for route in self._routes:
            full_path = join_paths(prefix, route.path) if prefix else route.path
            qualified = "{}.{}".format(namespace, route.name)
            resolved.append((full_path, route.handler, route.methods, qualified))

        for child, child_prefix in self._children:
            merged_prefix = join_paths(prefix, child_prefix) if child_prefix else prefix
            resolved.extend(child.iter_routes(merged_prefix, namespace))
        return resolved

    def register(self, app: Any, url_prefix: Optional[str] = None) -> None:
        """Attach every recorded route and middleware to ``app``.

        Parameters
        ----------
        app:
            Any object exposing ``add_route(name, path, handler, methods)`` and
            ``use(middleware)`` -- i.e. a :class:`~webcore.app.App`.
        url_prefix:
            An extra prefix layered above the blueprint's own ``url_prefix``.
        """
        base = url_prefix if url_prefix is not None else ""
        for middleware in self._middlewares:
            app.use(middleware)
        for full_path, handler, methods, qualified in self.iter_routes(base):
            app.add_route(qualified, full_path, handler, methods)

    def route_count(self) -> int:
        """Total number of routes, including nested blueprints."""
        return len(self.iter_routes())

    def __repr__(self) -> str:
        return "<Blueprint {!r} prefix={!r} routes={}>".format(
            self.name, self.url_prefix, len(self._routes)
        )
