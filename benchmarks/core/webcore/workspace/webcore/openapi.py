"""webcore.openapi -- describe an app's routes as an OpenAPI-ish document.

A lightweight, original schema builder that walks the routes an application (or a
list of route descriptors) exposes and emits a JSON-serialisable dict loosely
shaped like an OpenAPI 3 ``paths`` object: each route becomes a path with an
operation per HTTP method, path parameters typed from their converters. It is not
a full OpenAPI implementation -- no request/response body schemas -- but enough to
render a route index or feed a simple docs page.

The builder reads only the public route metadata (``name``, ``path``,
``methods``, ``handler``), so it stays decoupled from routing internals.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = ["OpenAPIBuilder", "route_to_operation", "convert_path", "PARAM_SCHEMAS"]

#: Placeholder pattern: ``{name}`` or ``{name:converter}``.
_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([a-zA-Z]+))?\}")

#: OpenAPI-ish type/format for each webcore converter.
PARAM_SCHEMAS: Dict[str, Dict[str, str]] = {
    "str": {"type": "string"},
    "int": {"type": "integer", "format": "int32"},
    "slug": {"type": "string", "format": "slug"},
    "path": {"type": "string", "format": "path"},
}


def convert_path(pattern: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Convert a webcore route ``pattern`` into an OpenAPI path + parameter list.

    ``"/users/{id:int}"`` -> ``("/users/{id}", [{"name": "id", ...}])``. Each
    parameter entry is a path-parameter object with a schema derived from its
    converter (defaulting to string).
    """
    parameters: List[Dict[str, Any]] = []

    def _replace(match: "re.Match") -> str:
        name = match.group(1)
        converter = match.group(2) or "str"
        schema = PARAM_SCHEMAS.get(converter, PARAM_SCHEMAS["str"])
        parameters.append({
            "name": name,
            "in": "path",
            "required": True,
            "schema": dict(schema),
        })
        return "{" + name + "}"

    openapi_path = _PARAM_RE.sub(_replace, pattern)
    return openapi_path, parameters


def _summary_from_handler(handler: Any) -> str:
    """Derive an operation summary from a handler's name or docstring."""
    doc = getattr(handler, "__doc__", None)
    if doc:
        return doc.strip().splitlines()[0].strip()
    name = getattr(handler, "__name__", "operation")
    return name.replace("_", " ").strip().capitalize()


def route_to_operation(name: str, pattern: str, methods: Iterable[str],
                       handler: Any = None) -> Tuple[str, Dict[str, Any]]:
    """Build the ``(path, path_item)`` pair for one route.

    The path item maps each lower-cased method to an operation object with an
    ``operationId`` (the route name), a ``summary``, and the shared
    ``parameters`` list.
    """
    openapi_path, parameters = convert_path(pattern)
    summary = _summary_from_handler(handler) if handler is not None else name
    path_item: Dict[str, Any] = {}
    if parameters:
        path_item["parameters"] = parameters
    for method in methods:
        verb = method.lower()
        if verb in ("head", "options"):
            continue  # auto methods are not documented as operations
        path_item[verb] = {
            "operationId": "{}_{}".format(name, verb),
            "summary": summary,
            "responses": {"200": {"description": "Successful response"}},
        }
    return openapi_path, path_item


class OpenAPIBuilder:
    """Assemble an OpenAPI-ish document from route descriptors.

    Parameters
    ----------
    title / version:
        The ``info`` block of the emitted document.

    Feed routes with :meth:`add_route` (or :meth:`add_app` to introspect an
    :class:`~webcore.app.App`'s router), then :meth:`build` the final ``dict``.
    """

    def __init__(self, title: str = "webcore application", version: str = "1.0.0") -> None:
        self.title = title
        self.version = version
        self._paths: Dict[str, Dict[str, Any]] = {}

    def add_route(self, name: str, pattern: str, methods: Iterable[str],
                  handler: Any = None) -> None:
        """Add one route's operations to the document, merging by path."""
        openapi_path, path_item = route_to_operation(name, pattern, methods, handler)
        existing = self._paths.setdefault(openapi_path, {})
        existing.update(path_item)

    def add_app(self, app: Any) -> None:
        """Introspect an app's router and add every route it exposes.

        Reads ``app.router.routes`` (a list of route objects with ``name``,
        ``path``, ``methods`` and ``handler`` attributes). Unknown router shapes
        are skipped silently, keeping the builder tolerant of internals.
        """
        router = getattr(app, "router", None)
        routes = getattr(router, "routes", None)
        if not routes:
            return
        for route in routes:
            name = getattr(route, "name", None)
            pattern = getattr(route, "path", None)
            methods = getattr(route, "methods", ("GET",))
            handler = getattr(route, "handler", None)
            if name and pattern:
                self.add_route(name, pattern, methods, handler)

    def build(self) -> Dict[str, Any]:
        """Return the assembled document as a plain ``dict``."""
        return {
            "openapi": "3.0.0",
            "info": {"title": self.title, "version": self.version},
            "paths": dict(self._paths),
        }

    def paths(self) -> List[str]:
        """The documented paths (sorted, for a stable index)."""
        return sorted(self._paths.keys())

    def __repr__(self) -> str:
        return "<OpenAPIBuilder {!r} paths={}>".format(self.title, len(self._paths))
