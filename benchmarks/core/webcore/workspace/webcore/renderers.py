"""webcore.renderers -- pluggable response renderers with content negotiation.

A handler often computes a plain Python value and wants the framework to serialise
it into whatever the client asked for -- JSON for an API client, HTML for a
browser. A :class:`Renderer` knows one media type and how to turn a value into a
:class:`~webcore.response.Response`; a :class:`RendererRegistry` negotiates the
best renderer from the request's ``Accept`` header and applies it.

This composes with the rest of webcore: renderers build on the ``text``/``html``/
``json_response`` helpers, and negotiation reuses :mod:`webcore.negotiation`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .negotiation import Accept
from .response import Response, html, json_response, text
from .templating import Environment

__all__ = [
    "Renderer",
    "JSONRenderer",
    "TextRenderer",
    "HTMLRenderer",
    "TemplateRenderer",
    "RendererRegistry",
]


class Renderer:
    """Base class: a media type plus a value-to-:class:`Response` method."""

    media_type = "application/octet-stream"

    def render(self, value: Any, status: int = 200) -> Response:  # pragma: no cover
        raise NotImplementedError

    def __repr__(self) -> str:
        return "<{} {}>".format(type(self).__name__, self.media_type)


class JSONRenderer(Renderer):
    """Serialise a value as ``application/json`` (compact, sorted keys)."""

    media_type = "application/json"

    def render(self, value: Any, status: int = 200) -> Response:
        return json_response(value, status=status)


class TextRenderer(Renderer):
    """Serialise a value as ``text/plain`` via ``str``."""

    media_type = "text/plain"

    def render(self, value: Any, status: int = 200) -> Response:
        return text("" if value is None else str(value), status=status)


class HTMLRenderer(Renderer):
    """Wrap a value as ``text/html`` (the value is assumed to be HTML text)."""

    media_type = "text/html"

    def render(self, value: Any, status: int = 200) -> Response:
        return html("" if value is None else str(value), status=status)


class TemplateRenderer(Renderer):
    """Render a context ``dict`` through a template into ``text/html``.

    Constructed with a template source string and an
    :class:`~webcore.templating.Environment`; :meth:`render` treats the value as
    the template context.
    """

    media_type = "text/html"

    def __init__(self, template: str, environment: Optional[Environment] = None) -> None:
        self.template = template
        self.environment = environment or Environment()

    def render(self, value: Any, status: int = 200) -> Response:
        context = value if isinstance(value, dict) else {"value": value}
        body = self.environment.render_string(self.template, **context)
        return html(body, status=status)


class RendererRegistry:
    """Negotiate and apply the best renderer for a request.

    Register renderers keyed by media type; :meth:`render_for` inspects a
    request's ``Accept`` header, picks the highest-quality offered media type,
    and renders the value with the matching renderer -- falling back to a default
    when the client accepts nothing on offer.
    """

    def __init__(self, default: str = "application/json") -> None:
        self._renderers: Dict[str, Renderer] = {}
        self.default_media_type = default
        self.register(JSONRenderer())
        self.register(TextRenderer())
        self.register(HTMLRenderer())

    def register(self, renderer: Renderer) -> None:
        """Add or replace the renderer for its media type."""
        self._renderers[renderer.media_type] = renderer

    def offered(self) -> List[str]:
        """The media types the registry can produce."""
        return list(self._renderers.keys())

    def select(self, accept_header: str) -> Renderer:
        """Choose a renderer for an ``Accept`` header value.

        Returns the default renderer when the header is empty or names nothing on
        offer.
        """
        accept = Accept.from_header(accept_header, media=True)
        if not accept:
            return self._renderers[self.default_media_type]
        best = accept.best_match(self.offered(), default=self.default_media_type)
        return self._renderers.get(best, self._renderers[self.default_media_type])

    def render_for(self, request, value: Any, status: int = 200) -> Response:
        """Negotiate from ``request`` and render ``value`` accordingly."""
        renderer = self.select(request.header("accept", ""))
        return renderer.render(value, status=status)

    def __repr__(self) -> str:
        return "<RendererRegistry {}>".format(sorted(self._renderers))
