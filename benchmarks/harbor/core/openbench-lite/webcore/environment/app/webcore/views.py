"""webcore.views -- class-based views.

Some endpoints are naturally a small class with one method per HTTP verb rather
than a single function. :class:`MethodView` provides that: subclass it, define
``get``/``post``/``delete``/... methods, and turn it into a webcore handler with
:meth:`MethodView.as_handler`. Dispatch picks the method matching the request's
verb (with automatic ``HEAD`` delegating to ``get``) and returns ``405`` for a
verb the class does not implement.

:class:`View` is the minimal base (a single :meth:`dispatch`); :class:`MethodView`
adds the verb-routing on top. A :class:`TemplateView` convenience renders a
template with context from :meth:`get_context`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .exceptions import MethodNotAllowed
from .response import Response, html

__all__ = ["View", "MethodView", "TemplateView"]

#: Verbs a :class:`MethodView` may implement, in canonical sort order.
_HTTP_METHODS = ("get", "head", "post", "put", "patch", "delete", "options")


class View:
    """The minimal class-based view: one :meth:`dispatch` per request.

    A subclass overrides :meth:`dispatch(request, **params)` and is exposed as a
    webcore handler via :meth:`as_handler`. Per-request state can live on the
    instance because :meth:`as_handler` constructs a fresh instance per call.
    """

    #: Class-level init kwargs, filled by :meth:`as_handler`.
    init_kwargs: Dict[str, Any] = {}

    def dispatch(self, request, **params):  # pragma: no cover - overridden
        raise NotImplementedError

    @classmethod
    def as_handler(cls, **init_kwargs) -> Callable:
        """Return a ``handler(request, **params)`` that instantiates and dispatches.

        Any ``init_kwargs`` are passed to the view's constructor on each request,
        letting one view class be mounted at several routes with different config.
        """
        def handler(request, **params):
            instance = cls(**init_kwargs)
            return instance.dispatch(request, **params)
        handler.view_class = cls  # type: ignore[attr-defined]
        handler.__name__ = cls.__name__
        return handler


class MethodView(View):
    """A view that routes to a method named after the request verb.

    Implement any of ``get``, ``post``, ``put``, ``patch``, ``delete``,
    ``options`` (each ``method(request, **params) -> Response``). A ``HEAD`` with
    no explicit handler falls back to ``get``. Unimplemented verbs raise
    :class:`~webcore.exceptions.MethodNotAllowed` carrying the ``Allow`` set.
    """

    def dispatch(self, request, **params):
        verb = request.method.lower()
        method = getattr(self, verb, None)
        if method is None and verb == "head":
            method = getattr(self, "get", None)
        if method is None:
            raise MethodNotAllowed(allowed_methods=self.allowed_methods())
        return method(request, **params)

    def allowed_methods(self) -> List[str]:
        """The upper-cased verbs this instance actually implements.

        ``HEAD`` is reported whenever ``get`` exists, and ``OPTIONS`` is always
        reported, mirroring webcore's automatic method handling.
        """
        allowed = set()
        for verb in _HTTP_METHODS:
            if callable(getattr(self, verb, None)):
                allowed.add(verb.upper())
        if "GET" in allowed:
            allowed.add("HEAD")
        allowed.add("OPTIONS")
        return sorted(allowed)

    def options(self, request, **params) -> Response:
        """Default ``OPTIONS`` handler: 204 with a sorted ``Allow`` header."""
        resp = Response(204, None, b"")
        resp.headers["Allow"] = ", ".join(self.allowed_methods())
        return resp


class TemplateView(MethodView):
    """A :class:`MethodView` that renders a template on ``GET``.

    Set :attr:`template` (a source string) and override :meth:`get_context` to
    supply variables. Requires an :class:`~webcore.templating.Environment`, passed
    as the ``environment`` init kwarg.
    """

    template: str = ""

    def __init__(self, environment=None, template: Optional[str] = None) -> None:
        from .templating import Environment
        self.environment = environment or Environment()
        if template is not None:
            self.template = template

    def get_context(self, request, **params) -> Dict[str, Any]:
        """Return the template context; override to inject data."""
        return dict(params)

    def get(self, request, **params) -> Response:
        context = self.get_context(request, **params)
        body = self.environment.render_string(self.template, **context)
        return html(body)
