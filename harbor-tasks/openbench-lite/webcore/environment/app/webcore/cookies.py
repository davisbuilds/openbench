"""webcore.cookies -- cookie header parsing and building.

The response object already offers a minimal ``set_cookie``; this module gives
the framework a fuller, standalone cookie toolkit for the cases that need it:
parsing a ``Cookie:`` request header into a mapping, rendering a well-formed
``Set-Cookie`` value with every common attribute, and a small :class:`CookieJar`
that accumulates several cookies and applies them to a response.

Nothing here is wired into dispatch automatically -- it is a utility layer that
:mod:`webcore.sessions` and application code build on.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Union

from .datetimeutil import cookie_date

__all__ = [
    "parse_cookie",
    "dump_cookie",
    "Cookie",
    "CookieJar",
]

#: SameSite values a cookie may declare.
_SAME_SITE_VALUES = frozenset({"Strict", "Lax", "None"})


def parse_cookie(header: str) -> Dict[str, str]:
    """Parse a ``Cookie:`` request header into a ``dict`` (last duplicate wins).

    ``"a=1; b=2"`` -> ``{"a": "1", "b": "2"}``. Segments without ``=`` are
    skipped; surrounding double quotes on a value are stripped.
    """
    cookies: Dict[str, str] = {}
    if not header:
        return cookies
    for chunk in header.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        cookies[name] = value
    return cookies


def dump_cookie(name: str, value: str = "", *, max_age: Optional[int] = None,
                expires: Optional[Union[int, float]] = None, path: str = "/",
                domain: Optional[str] = None, secure: bool = False,
                http_only: bool = False,
                same_site: Optional[str] = None) -> str:
    """Render a single ``Set-Cookie`` header *value* (without the header name).

    Attributes are emitted in the conventional order
    (``name=value; Expires; Max-Age; Domain; Path; SameSite; Secure; HttpOnly``).
    An invalid ``same_site`` raises :class:`ValueError` early.
    """
    if same_site is not None and same_site not in _SAME_SITE_VALUES:
        raise ValueError("invalid SameSite value {!r}".format(same_site))
    parts: List[str] = ["{}={}".format(name, value)]
    if expires is not None:
        parts.append("Expires=" + cookie_date(expires))
    if max_age is not None:
        parts.append("Max-Age=" + str(int(max_age)))
    if domain:
        parts.append("Domain=" + domain)
    if path:
        parts.append("Path=" + path)
    if same_site:
        parts.append("SameSite=" + same_site)
    if secure:
        parts.append("Secure")
    if http_only:
        parts.append("HttpOnly")
    return "; ".join(parts)


class Cookie:
    """A cookie's name/value and attributes, renderable to a header value."""

    def __init__(self, name: str, value: str = "", *, max_age: Optional[int] = None,
                 expires: Optional[Union[int, float]] = None, path: str = "/",
                 domain: Optional[str] = None, secure: bool = False,
                 http_only: bool = False, same_site: Optional[str] = None) -> None:
        self.name = name
        self.value = value
        self.max_age = max_age
        self.expires = expires
        self.path = path
        self.domain = domain
        self.secure = secure
        self.http_only = http_only
        self.same_site = same_site

    def to_header(self) -> str:
        """Render this cookie as a ``Set-Cookie`` value."""
        return dump_cookie(
            self.name, self.value, max_age=self.max_age, expires=self.expires,
            path=self.path, domain=self.domain, secure=self.secure,
            http_only=self.http_only, same_site=self.same_site,
        )

    @classmethod
    def deletion(cls, name: str, path: str = "/") -> "Cookie":
        """A cookie that instructs the client to delete ``name`` (``Max-Age=0``)."""
        return cls(name, "", max_age=0, path=path)

    def __repr__(self) -> str:
        return "<Cookie {}={!r}>".format(self.name, self.value)


class CookieJar:
    """Collect several :class:`Cookie` objects and apply them to a response.

    Because the minimal :class:`~webcore.response.Headers` keeps one value per
    name, applying multiple cookies uses the response's own ``set_cookie`` per
    cookie; the jar is mainly a convenient accumulator with de-duplication by
    ``(name, path)``.
    """

    def __init__(self) -> None:
        self._cookies: "Dict[tuple, Cookie]" = {}

    def set(self, name: str, value: str = "", **attrs) -> Cookie:
        """Add or replace a cookie and return it."""
        cookie = Cookie(name, value, **attrs)
        self._cookies[(name, cookie.path)] = cookie
        return cookie

    def delete(self, name: str, path: str = "/") -> Cookie:
        """Queue a deletion cookie for ``name``."""
        cookie = Cookie.deletion(name, path)
        self._cookies[(name, path)] = cookie
        return cookie

    def headers(self) -> List[str]:
        """Every queued cookie rendered as a ``Set-Cookie`` value."""
        return [cookie.to_header() for cookie in self._cookies.values()]

    def apply(self, response) -> None:
        """Write each queued cookie onto ``response`` via its ``set_cookie``."""
        for cookie in self._cookies.values():
            response.set_cookie(
                cookie.name, cookie.value, path=cookie.path,
                max_age=cookie.max_age, http_only=cookie.http_only,
                secure=cookie.secure, same_site=cookie.same_site,
            )

    def __len__(self) -> int:
        return len(self._cookies)

    def __repr__(self) -> str:
        return "<CookieJar {} cookies>".format(len(self._cookies))
