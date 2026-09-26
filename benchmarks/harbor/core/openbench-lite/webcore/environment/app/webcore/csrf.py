"""webcore.csrf -- cross-site request forgery protection.

The double-submit / signed-token pattern: the server issues a signed token, the
client returns it on unsafe requests (via a header or form field), and the
middleware verifies the signature before the handler runs. Tokens are minted and
checked with :class:`webcore.signing.TimedSigner`, so they carry an age and no
server-side store is needed.

Safe methods (``GET``/``HEAD``/``OPTIONS``) are never challenged; unsafe ones
(``POST``/``PUT``/``PATCH``/``DELETE``) must present a valid, unexpired token or
the middleware short-circuits with ``403``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from .exceptions import Forbidden
from .signing import BadSignature, TimedSigner

__all__ = [
    "CSRFProtect",
    "generate_token",
    "validate_token",
    "SAFE_METHODS",
]

#: Methods that never require a CSRF token (they must not change server state).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def generate_token(signer: TimedSigner, session_id: str = "") -> str:
    """Mint a signed CSRF token, optionally bound to a ``session_id``.

    Binding to a session id makes a leaked token useless in another session;
    pass ``""`` for the simpler unbound variant.
    """
    return signer.sign("csrf:" + session_id)


def validate_token(signer: TimedSigner, token: str, session_id: str = "",
                   max_age: Optional[int] = 3600) -> bool:
    """Return whether ``token`` is a valid, unexpired token for ``session_id``."""
    if not token:
        return False
    try:
        value = signer.unsign(token, max_age=max_age)
    except BadSignature:
        return False
    return value == "csrf:" + session_id


class CSRFProtect:
    """Middleware that rejects unsafe requests without a valid CSRF token.

    Parameters
    ----------
    secret:
        Signing key for the token :class:`~webcore.signing.TimedSigner`.
    header_name:
        Request header carrying the token (default ``X-CSRF-Token``).
    field_name:
        Form field consulted as a fallback when the header is absent.
    max_age:
        Token lifetime in seconds.
    exempt:
        Path prefixes that skip the check entirely (e.g. a webhook endpoint).

    The middleware attaches a freshly-minted token to the request as
    ``request.csrf_token`` so a handler/template can echo it back to the client.
    """

    def __init__(self, secret: str, header_name: str = "X-CSRF-Token",
                 field_name: str = "csrf_token", max_age: int = 3600,
                 exempt: Sequence[str] = ()) -> None:
        self.signer = TimedSigner(secret, salt="webcore.csrf")
        self.header_name = header_name
        self.field_name = field_name
        self.max_age = max_age
        self.exempt = tuple(exempt)

    def issue(self, session_id: str = "") -> str:
        """Mint a token for the given session id (or unbound)."""
        return generate_token(self.signer, session_id)

    def _is_exempt(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.exempt)

    def _extract_token(self, request) -> str:
        header = request.header(self.header_name)
        if header:
            return header
        # Fall back to a form field for classic HTML form posts.
        try:
            form = request.form()
        except Exception:
            return ""
        return form.get(self.field_name, "") if form is not None else ""

    def __call__(self, request, next_call):
        setattr(request, "csrf_token", self.issue())
        if request.method in SAFE_METHODS or self._is_exempt(request.path):
            return next_call(request)
        token = self._extract_token(request)
        if not validate_token(self.signer, token, "", self.max_age):
            raise Forbidden(description="CSRF token missing or invalid")
        return next_call(request)

    def __repr__(self) -> str:
        return "<CSRFProtect header={!r}>".format(self.header_name)
