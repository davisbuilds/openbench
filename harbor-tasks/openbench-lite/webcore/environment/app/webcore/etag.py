"""webcore.etag -- entity tags and conditional-request handling.

ETags let a client revalidate a cached representation cheaply: the server tags a
response body, the client sends the tag back in ``If-None-Match``, and an
unchanged resource is answered with ``304 Not Modified`` and no body. This module
computes tags, parses the conditional headers, and offers a middleware that
applies the whole dance automatically to safe requests.

Tags are content hashes by default (strong tags); a ``weak=True`` option emits
the ``W/"..."`` weak form for representations that are semantically-but-not-byte
equivalent.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional

from .response import Response
from .status import has_body

__all__ = [
    "compute_etag",
    "parse_if_none_match",
    "etag_matches",
    "ConditionalMiddleware",
]


def compute_etag(body: bytes, weak: bool = False) -> str:
    """Compute an ETag for ``body`` as a quoted SHA-1 hex digest.

    ``weak=True`` prefixes the ``W/`` weak-validator marker. The digest is
    truncated to 32 hex chars -- ample to avoid collisions for cache validation
    while keeping the header compact.
    """
    digest = hashlib.sha1(body).hexdigest()[:32]
    tag = '"{}"'.format(digest)
    return "W/" + tag if weak else tag


def parse_if_none_match(header: Optional[str]) -> List[str]:
    """Parse an ``If-None-Match`` header into a list of tags.

    ``*`` is returned as a single-element ``["*"]``. Weak/strong markers are kept
    verbatim so :func:`etag_matches` can compare them.
    """
    if not header:
        return []
    header = header.strip()
    if header == "*":
        return ["*"]
    return [tag.strip() for tag in header.split(",") if tag.strip()]


def _normalise(tag: str) -> str:
    """Strip a weak marker for weak comparison (RFC 7232 §2.3.2)."""
    return tag[2:] if tag.startswith("W/") else tag


def etag_matches(current: str, candidates: List[str], weak: bool = True) -> bool:
    """Return whether ``current`` matches any of ``candidates``.

    ``*`` matches anything. With ``weak=True`` (the default for ``If-None-Match``)
    the comparison ignores the ``W/`` marker; strong comparison requires an exact
    byte match.
    """
    if not candidates:
        return False
    if "*" in candidates:
        return True
    if weak:
        current_n = _normalise(current)
        return any(_normalise(tag) == current_n for tag in candidates)
    return current in candidates


class ConditionalMiddleware:
    """Middleware that adds ETags and answers ``304`` for matching requests.

    On the way out it computes an ETag for the response body (unless the handler
    already set one) and, for a safe request whose ``If-None-Match`` matches,
    replaces the response with a bodyless ``304`` carrying the same ``ETag``.

    Parameters
    ----------
    weak:
        Emit weak ETags.
    methods:
        Request methods eligible for revalidation (default ``GET``/``HEAD``).
    """

    def __init__(self, weak: bool = False,
                 methods: tuple = ("GET", "HEAD")) -> None:
        self.weak = weak
        self.methods = tuple(methods)

    def __call__(self, request, next_call):
        response = next_call(request)
        if not self._eligible(request, response):
            return response
        etag = response.headers.get("ETag") or compute_etag(response.body_bytes(), self.weak)
        response.headers["ETag"] = etag
        candidates = parse_if_none_match(request.header("if-none-match"))
        if etag_matches(etag, candidates):
            return self._not_modified(etag, response)
        return response

    def _eligible(self, request, response) -> bool:
        if request.method not in self.methods:
            return False
        if not hasattr(response, "headers"):
            return False
        return has_body(response.status) and 200 <= response.status < 300

    @staticmethod
    def _not_modified(etag: str, original: Response) -> Response:
        resp = Response(304, None, b"")
        resp.headers["ETag"] = etag
        for carry in ("Cache-Control", "Last-Modified", "Vary"):
            if carry in original.headers:
                resp.headers[carry] = original.headers[carry]
        return resp

    def __repr__(self) -> str:
        return "<ConditionalMiddleware weak={}>".format(self.weak)
