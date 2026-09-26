"""webcore.method_override -- let clients tunnel verbs HTML forms cannot send.

HTML forms can only issue ``GET`` and ``POST``, yet RESTful handlers want
``PUT``/``PATCH``/``DELETE``. The conventional workaround is to send a ``POST``
carrying the intended method in a hidden ``_method`` form field or an
``X-HTTP-Method-Override`` header; this adapter rewrites the request's method
*before routing runs*, so the tunnelled verb selects the right handler.

Because webcore resolves the route before the middleware onion runs, method
override cannot be an ordinary ``app.use`` middleware (that runs too late to
influence routing). Instead :class:`MethodOverride` **wraps an app** and exposes a
``dispatch`` of its own, rewriting the request first and then delegating -- the
same wrapping pattern :class:`webcore.wsgi.WSGIAdapter` uses.

Only an overriding ``POST`` is honoured, and only to a safe allow-list of target
methods, so the mechanism cannot forge arbitrary verbs.

Example
-------
::

    app = App()

    @app.route("/items/{id:int}", methods=["DELETE"])
    def delete_item(request, id):
        ...

    wrapped = MethodOverride(app)
    client = TestClient(wrapped)
    client.post("/items/7", headers={"X-HTTP-Method-Override": "DELETE"})
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

__all__ = ["MethodOverride", "resolve_override"]

#: Methods a POST may be rewritten to.
_ALLOWED_TARGETS = frozenset({"PUT", "PATCH", "DELETE"})


def resolve_override(request, header_name: str, field_name: str,
                     allowed: Iterable[str]) -> Optional[str]:
    """Return the overriding method for ``request`` if one is validly requested.

    Checks the override header first, then the form field. Returns the upper-cased
    target method when it is in ``allowed`` and the request is a ``POST``;
    otherwise ``None`` (no override).
    """
    if request.method != "POST":
        return None
    allowed_set = {m.upper() for m in allowed}
    candidate = request.header(header_name)
    if not candidate:
        try:
            form = request.form()
            candidate = form.get(field_name) if form is not None else None
        except Exception:
            candidate = None
    if not candidate:
        return None
    candidate = candidate.strip().upper()
    return candidate if candidate in allowed_set else None


class MethodOverride:
    """Wrap an app so a tunnelled verb is applied before routing.

    Parameters
    ----------
    app:
        The wrapped application; must expose ``dispatch(request) -> response``.
    header_name:
        Header carrying the desired method (default ``X-HTTP-Method-Override``).
    field_name:
        Form field consulted when the header is absent (default ``_method``).
    allowed:
        The set of methods a ``POST`` may be rewritten to (default
        ``PUT``/``PATCH``/``DELETE``).

    The wrapper is itself a drop-in target for
    :class:`~webcore.testclient.TestClient` because it presents the same
    ``dispatch`` signature. The original method is preserved on the rewritten
    request as ``request.original_method`` for auditing.
    """

    def __init__(self, app: Any, header_name: str = "X-HTTP-Method-Override",
                 field_name: str = "_method",
                 allowed: Iterable[str] = _ALLOWED_TARGETS) -> None:
        self.app = app
        self.header_name = header_name
        self.field_name = field_name
        self.allowed = frozenset(m.upper() for m in allowed)

    def apply(self, request):
        """Rewrite ``request.method`` in place if a valid override is present.

        Returns the (possibly-mutated) request so it can be used in a pipeline.
        """
        target = resolve_override(request, self.header_name, self.field_name, self.allowed)
        if target is not None:
            setattr(request, "original_method", request.method)
            request.method = target
        return request

    def dispatch(self, request):
        """Apply the override then delegate to the wrapped app's ``dispatch``."""
        return self.app.dispatch(self.apply(request))

    def __repr__(self) -> str:
        return "<MethodOverride header={!r}>".format(self.header_name)
