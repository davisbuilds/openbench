"""webcore.security -- security-header middleware and small helpers.

A single middleware that sets the common hardening response headers -- HSTS,
``X-Content-Type-Options``, ``X-Frame-Options``, ``Referrer-Policy`` and a
``Content-Security-Policy`` -- with sensible defaults that an application can
tune. Also includes a tiny :class:`ContentSecurityPolicy` builder so the CSP is
constructed structurally instead of by string concatenation.

Everything is opt-in via ``app.use(SecurityHeaders())`` and only *adds* headers,
never removing or overriding ones a handler set deliberately (existing values
win).
"""

from __future__ import annotations

from typing import Dict, List, Optional

__all__ = [
    "SecurityHeaders",
    "ContentSecurityPolicy",
    "DEFAULT_HEADERS",
]

#: The headers :class:`SecurityHeaders` applies unless overridden.
DEFAULT_HEADERS: Dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-XSS-Protection": "0",
}


class ContentSecurityPolicy:
    """Build a ``Content-Security-Policy`` value from named directives.

    ::

        ContentSecurityPolicy().default("'self'").img("'self'", "data:").build()
        # "default-src 'self'; img-src 'self' data:"
    """

    def __init__(self) -> None:
        self._directives: "Dict[str, List[str]]" = {}

    def add(self, directive: str, *sources: str) -> "ContentSecurityPolicy":
        """Append ``sources`` to a raw ``directive`` name (e.g. ``script-src``)."""
        self._directives.setdefault(directive, []).extend(sources)
        return self

    def default(self, *sources: str) -> "ContentSecurityPolicy":
        return self.add("default-src", *sources)

    def script(self, *sources: str) -> "ContentSecurityPolicy":
        return self.add("script-src", *sources)

    def style(self, *sources: str) -> "ContentSecurityPolicy":
        return self.add("style-src", *sources)

    def img(self, *sources: str) -> "ContentSecurityPolicy":
        return self.add("img-src", *sources)

    def connect(self, *sources: str) -> "ContentSecurityPolicy":
        return self.add("connect-src", *sources)

    def build(self) -> str:
        """Render the directives into a single header value (insertion order)."""
        parts = []
        for directive, sources in self._directives.items():
            if sources:
                parts.append("{} {}".format(directive, " ".join(sources)))
            else:
                parts.append(directive)
        return "; ".join(parts)

    def __str__(self) -> str:
        return self.build()


class SecurityHeaders:
    """Middleware that adds hardening headers to every response.

    Parameters
    ----------
    headers:
        A base header map (defaults to :data:`DEFAULT_HEADERS`); merged over, not
        replacing, per-response headers.
    hsts_max_age:
        When set, adds ``Strict-Transport-Security`` with this ``max-age``.
    hsts_include_subdomains:
        Append ``includeSubDomains`` to the HSTS header.
    csp:
        A :class:`ContentSecurityPolicy` (or raw string) to emit as
        ``Content-Security-Policy``.
    frame_options:
        Override ``X-Frame-Options`` (``DENY``/``SAMEORIGIN``), or ``None`` to
        omit it.
    """

    def __init__(self, headers: Optional[Dict[str, str]] = None,
                 hsts_max_age: Optional[int] = None,
                 hsts_include_subdomains: bool = False,
                 csp=None, frame_options: Optional[str] = "DENY") -> None:
        self.headers = dict(headers) if headers is not None else dict(DEFAULT_HEADERS)
        if frame_options is None:
            self.headers.pop("X-Frame-Options", None)
        else:
            self.headers["X-Frame-Options"] = frame_options
        if hsts_max_age is not None:
            value = "max-age={}".format(int(hsts_max_age))
            if hsts_include_subdomains:
                value += "; includeSubDomains"
            self.headers["Strict-Transport-Security"] = value
        if csp is not None:
            self.headers["Content-Security-Policy"] = str(csp)

    def __call__(self, request, next_call):
        response = next_call(request)
        if hasattr(response, "headers"):
            for name, value in self.headers.items():
                # Do not clobber a header the handler set on purpose.
                if name not in response.headers:
                    response.headers[name] = value
        return response

    def __repr__(self) -> str:
        return "<SecurityHeaders {} headers>".format(len(self.headers))
