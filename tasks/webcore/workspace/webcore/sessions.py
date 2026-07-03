"""webcore.sessions -- signed-cookie sessions.

A *session* is a small mutable mapping that survives across requests by riding
in a cookie. webcore keeps the whole session **in the cookie itself** (there is
no server-side store): the payload is JSON, then signed -- not encrypted -- with
:class:`webcore.signing.TimedSigner`, so the client can read it but cannot forge
it, and a stale cookie is rejected once it passes ``max_age``.

Typical wiring is through :class:`SessionMiddleware`, which loads the session
before the handler runs and writes an updated ``Set-Cookie`` on the way out::

    app = App()
    app.use(SessionMiddleware("secret-key"))

    @app.route("/hit")
    def hit(request):
        session = request_session(request)
        session["count"] = session.get("count", 0) + 1
        return {"count": session["count"]}

The session is stashed on the request via a private attribute; ``request_session``
retrieves it. Only sessions that were *modified* emit a fresh cookie, which keeps
responses cache-friendly.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, MutableMapping, Optional

from .signing import BadSignature, TimedSigner

__all__ = [
    "Session",
    "SessionSerializer",
    "SessionMiddleware",
    "request_session",
]

#: Attribute name used to stash a loaded session on a request object.
_SESSION_ATTR = "_webcore_session"


class Session(MutableMapping):
    """A dict-like session with dirty-tracking.

    Behaves like a normal mutable mapping; every mutation flips :attr:`modified`
    so the middleware knows whether a new cookie is required. :attr:`new`
    distinguishes a session that was just created from one loaded from a valid
    cookie.
    """

    def __init__(self, data: Optional[Dict[str, Any]] = None, new: bool = True) -> None:
        self._data: Dict[str, Any] = dict(data or {})
        self.new = new
        self.modified = False
        #: Set to True to instruct the middleware to delete the cookie.
        self.cleared = False

    # -- MutableMapping protocol ----------------------------------------

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.modified = True

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        self.modified = True

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    # -- convenience -----------------------------------------------------

    def clear(self) -> None:
        """Empty the session and mark it for cookie deletion."""
        if self._data:
            self.modified = True
        self._data.clear()
        self.cleared = True

    def pop(self, key: str, *default: Any) -> Any:
        self.modified = True
        return self._data.pop(key, *default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self._data:
            self.modified = True
        return self._data.setdefault(key, default)

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._data.update(*args, **kwargs)
        self.modified = True

    def flash(self, message: str, category: str = "message") -> None:
        """Queue a one-shot *flash* message under ``category``.

        Flashes accumulate in a list on the session and are meant to be consumed
        (and cleared) by the next request's template; see :meth:`pop_flashes`.
        """
        bucket = self._data.setdefault("_flashes", [])
        bucket.append([category, message])
        self.modified = True

    def pop_flashes(self, category: Optional[str] = None) -> list:
        """Return and remove queued flashes, optionally filtered by category."""
        bucket = self._data.get("_flashes", [])
        if not bucket:
            return []
        if category is None:
            kept, taken = [], bucket
        else:
            kept = [pair for pair in bucket if pair[0] != category]
            taken = [pair for pair in bucket if pair[0] == category]
        if kept:
            self._data["_flashes"] = kept
        else:
            self._data.pop("_flashes", None)
        self.modified = True
        return [message for _category, message in taken]

    def to_dict(self) -> Dict[str, Any]:
        """A shallow copy of the underlying mapping."""
        return dict(self._data)

    def __repr__(self) -> str:
        state = "new" if self.new else "loaded"
        return "<Session {} keys={}>".format(state, sorted(self._data))


class SessionSerializer:
    """Turn a :class:`Session` into a signed cookie value and back.

    The payload is compact JSON (sorted keys for determinism) run through a
    :class:`~webcore.signing.TimedSigner`, so a tampered or expired cookie
    round-trips to a fresh, empty session rather than raising into the handler.
    """

    def __init__(self, secret: str, salt: str = "webcore.session",
                 max_age: Optional[int] = 14 * 24 * 3600) -> None:
        self.signer = TimedSigner(secret, salt=salt)
        self.max_age = max_age

    def dumps(self, session: Session) -> str:
        """Serialise and sign ``session`` into a cookie-safe string."""
        payload = json.dumps(session.to_dict(), sort_keys=True, separators=(",", ":"))
        return self.signer.sign(payload)

    def loads(self, cookie_value: str) -> Session:
        """Verify and decode a cookie value into a :class:`Session`.

        A missing, malformed, forged, or expired value yields a fresh empty
        session (``new=True``) -- loading never raises.
        """
        if not cookie_value:
            return Session(new=True)
        try:
            raw = self.signer.unsign(cookie_value, max_age=self.max_age)
            data = json.loads(raw)
        except (BadSignature, ValueError, TypeError):
            return Session(new=True)
        if not isinstance(data, dict):
            return Session(new=True)
        return Session(data, new=False)


class SessionMiddleware:
    """Load a signed-cookie session in, persist it out.

    Slots into the middleware onion (``app.use(SessionMiddleware(secret))``). On
    the way in it parses the configured cookie and attaches a :class:`Session`
    to the request; on the way out, if the session was modified, it appends a
    ``Set-Cookie`` with the re-signed payload. A cleared session emits a deletion
    cookie (``Max-Age=0``).
    """

    def __init__(self, secret: str, cookie_name: str = "session",
                 max_age: Optional[int] = 14 * 24 * 3600, path: str = "/",
                 http_only: bool = True, same_site: str = "Lax",
                 secure: bool = False) -> None:
        self.serializer = SessionSerializer(secret, max_age=max_age)
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.path = path
        self.http_only = http_only
        self.same_site = same_site
        self.secure = secure

    def __call__(self, request, next_call):
        cookie_value = request.cookies.get(self.cookie_name, "")
        session = self.serializer.loads(cookie_value)
        setattr(request, _SESSION_ATTR, session)

        response = next_call(request)

        if session.cleared:
            self._delete_cookie(response)
        elif session.modified:
            self._write_cookie(response, session)
        return response

    def _write_cookie(self, response, session: Session) -> None:
        response.set_cookie(
            self.cookie_name,
            self.serializer.dumps(session),
            path=self.path,
            max_age=self.max_age,
            http_only=self.http_only,
            secure=self.secure,
            same_site=self.same_site,
        )

    def _delete_cookie(self, response) -> None:
        response.set_cookie(
            self.cookie_name,
            "",
            path=self.path,
            max_age=0,
            http_only=self.http_only,
            secure=self.secure,
            same_site=self.same_site,
        )

    def __repr__(self) -> str:
        return "<SessionMiddleware cookie={!r}>".format(self.cookie_name)


def request_session(request) -> Session:
    """Return the :class:`Session` attached to ``request`` by the middleware.

    If no :class:`SessionMiddleware` ran (so nothing was attached), a detached,
    empty session is returned so handler code can stay uniform -- but changes to
    it will not be persisted.
    """
    session = getattr(request, _SESSION_ATTR, None)
    if session is None:
        session = Session(new=True)
        setattr(request, _SESSION_ATTR, session)
    return session
