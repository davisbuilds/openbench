"""webcore.debug -- traceback capture and a developer error page.

When a handler raises during development you want a readable page, not a bare
500. This module captures an exception into a structured :class:`Traceback`
(frames, line context, exception chain) and renders it as either plain text or a
simple HTML page. A :class:`DebugMiddleware` wraps dispatch so unhandled
exceptions become a 500 debug page instead of propagating.

Everything here is meant for development only -- the rendered page exposes source
and locals -- so :class:`DebugMiddleware` defaults to *off* unless explicitly
enabled.
"""

from __future__ import annotations

import traceback as _traceback
from types import TracebackType
from typing import Any, List, Optional, Tuple

from .response import Response, html, text
from .templating import escape

__all__ = ["Frame", "Traceback", "DebugMiddleware", "render_traceback_html"]


class Frame:
    """One stack frame: file, line number, function, and the source line."""

    __slots__ = ("filename", "lineno", "function", "line", "locals")

    def __init__(self, filename: str, lineno: int, function: str,
                 line: str, locals_: Optional[dict] = None) -> None:
        self.filename = filename
        self.lineno = lineno
        self.function = function
        self.line = line
        self.locals = locals_ or {}

    def location(self) -> str:
        """A ``file:line (function)`` label for the frame."""
        return "{}:{} ({})".format(self.filename, self.lineno, self.function)

    def __repr__(self) -> str:
        return "<Frame {}>".format(self.location())


class Traceback:
    """A captured exception: its type, message, and ordered frames.

    Build one with :meth:`from_exception`. :meth:`as_text` and :meth:`as_html`
    render it; :attr:`frames` is available for a custom presentation.
    """

    def __init__(self, exc_type: type, exc_value: BaseException,
                 frames: List[Frame]) -> None:
        self.exc_type = exc_type
        self.exc_value = exc_value
        self.frames = frames

    @classmethod
    def from_exception(cls, exc: BaseException,
                       capture_locals: bool = False) -> "Traceback":
        """Capture ``exc`` (using its own ``__traceback__``) into frames."""
        tb: Optional[TracebackType] = exc.__traceback__
        frames: List[Frame] = []
        for frame, lineno in _traceback.walk_tb(tb):
            code = frame.f_code
            source_line = _read_source_line(code.co_filename, lineno)
            locals_ = _safe_locals(frame.f_locals) if capture_locals else {}
            frames.append(Frame(code.co_filename, lineno, code.co_name,
                                source_line, locals_))
        return cls(type(exc), exc, frames)

    @property
    def summary(self) -> str:
        """A one-line ``ExcType: message`` summary."""
        return "{}: {}".format(self.exc_type.__name__, self.exc_value)

    def as_text(self) -> str:
        """Render a plain-text traceback (most-recent call last)."""
        lines = ["Traceback (most recent call last):"]
        for frame in self.frames:
            lines.append('  File "{}", line {}, in {}'.format(
                frame.filename, frame.lineno, frame.function))
            if frame.line:
                lines.append("    " + frame.line.strip())
        lines.append(self.summary)
        return "\n".join(lines)

    def as_html(self) -> str:
        """Render a minimal HTML debug page for this traceback."""
        return render_traceback_html(self)

    def __repr__(self) -> str:
        return "<Traceback {} ({} frames)>".format(
            self.exc_type.__name__, len(self.frames))


def _read_source_line(filename: str, lineno: int) -> str:
    """Best-effort read of a single source line (empty string on failure)."""
    try:
        import linecache
        return linecache.getline(filename, lineno).rstrip("\n")
    except Exception:  # pragma: no cover - defensive
        return ""


def _safe_locals(mapping: dict) -> dict:
    """Repr each local safely so an unprintable value cannot break rendering."""
    safe = {}
    for key, value in mapping.items():
        try:
            safe[key] = repr(value)
        except Exception:  # pragma: no cover - defensive
            safe[key] = "<unrepresentable>"
    return safe


def render_traceback_html(tb: Traceback) -> str:
    """Render a :class:`Traceback` as a standalone HTML page."""
    rows = []
    for frame in tb.frames:
        rows.append(
            "<li><code>{}</code><br><pre>{}</pre></li>".format(
                escape(frame.location()), escape(frame.line.strip()))
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>{title}</title></head><body>"
        "<h1>Unhandled exception</h1>"
        "<h2>{summary}</h2>"
        "<ol>{frames}</ol>"
        "<hr><small>webcore debug</small>"
        "</body></html>"
    ).format(
        title=escape(tb.exc_type.__name__),
        summary=escape(tb.summary),
        frames="".join(rows),
    )


class DebugMiddleware:
    """Middleware that turns an unhandled exception into a 500 debug response.

    Parameters
    ----------
    enabled:
        When false (the default), exceptions propagate unchanged -- production
        behaviour. Enable only in development.
    as_html:
        Render the debug page as HTML (``True``) or plain text (``False``).
    capture_locals:
        Include a repr of each frame's locals in the captured traceback.
    """

    def __init__(self, enabled: bool = False, as_html: bool = True,
                 capture_locals: bool = False) -> None:
        self.enabled = enabled
        self.as_html = as_html
        self.capture_locals = capture_locals

    def __call__(self, request, next_call):
        if not self.enabled:
            return next_call(request)
        try:
            return next_call(request)
        except Exception as exc:
            tb = Traceback.from_exception(exc, self.capture_locals)
            if self.as_html:
                return html(tb.as_html(), status=500)
            return text(tb.as_text(), status=500)

    def __repr__(self) -> str:
        return "<DebugMiddleware enabled={}>".format(self.enabled)
