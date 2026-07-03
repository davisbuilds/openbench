"""webcore.compression -- gzip response compression middleware.

A middleware that gzip-compresses eligible response bodies when the client
advertises ``Accept-Encoding: gzip``. It respects the usual guards: it skips
already-encoded responses, bodiless status codes, small bodies below a
threshold, and content types that do not benefit from compression (images,
video, already-compressed archives).

Compression uses only :mod:`gzip`/:mod:`zlib` from the stdlib. The middleware
sets ``Content-Encoding: gzip``, refreshes ``Content-Length``, and appends
``Vary: Accept-Encoding`` so caches key correctly.
"""

from __future__ import annotations

import gzip
from typing import Iterable, Optional

from .negotiation import Accept
from .status import has_body

__all__ = ["GzipMiddleware", "compress", "should_compress"]

#: Content-type prefixes that are already compressed; skip them.
_INCOMPRESSIBLE_PREFIXES = (
    "image/",
    "video/",
    "audio/",
    "application/zip",
    "application/gzip",
    "application/x-brotli",
    "font/woff",
)


def compress(data: bytes, level: int = 6) -> bytes:
    """Return the gzip-compressed form of ``data`` at the given ``level``."""
    return gzip.compress(data, compresslevel=level)


def should_compress(content_type: str, body_length: int, minimum: int) -> bool:
    """Decide whether a body of this type and size is worth compressing.

    ``True`` only when the body meets the size floor and its media type is not in
    the incompressible list. An empty content type is treated as compressible
    (text-like) since webcore text/JSON helpers always set one.
    """
    if body_length < minimum:
        return False
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return True
    return not any(ct.startswith(prefix) for prefix in _INCOMPRESSIBLE_PREFIXES)


class GzipMiddleware:
    """Compress response bodies for clients that accept gzip.

    Parameters
    ----------
    minimum_size:
        Bodies smaller than this (bytes) are left uncompressed -- gzip framing
        overhead makes tiny payloads larger.
    level:
        gzip compression level 0-9 (default 6, the usual speed/ratio balance).
    """

    def __init__(self, minimum_size: int = 500, level: int = 6) -> None:
        self.minimum_size = minimum_size
        self.level = level

    def _client_accepts_gzip(self, request) -> bool:
        accept = Accept.from_header(request.header("accept-encoding", ""))
        # An empty Accept-Encoding here means "no preference stated" -> skip,
        # because sending gzip to a client that did not ask risks breakage.
        if not accept:
            return False
        return accept.quality("gzip") > 0.0

    def __call__(self, request, next_call):
        response = next_call(request)
        if not self._eligible(request, response):
            return response
        raw = response.body_bytes()
        if not should_compress(
            response.headers.get("Content-Type", ""), len(raw), self.minimum_size
        ):
            self._mark_vary(response)
            return response
        response.body = compress(raw, self.level)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(response.body))
        self._mark_vary(response)
        return response

    def _eligible(self, request, response) -> bool:
        if not hasattr(response, "headers"):
            return False
        if "Content-Encoding" in response.headers:
            return False  # already encoded
        if not has_body(response.status):
            return False
        return self._client_accepts_gzip(request)

    @staticmethod
    def _mark_vary(response) -> None:
        existing = response.headers.get("Vary")
        if not existing:
            response.headers["Vary"] = "Accept-Encoding"
        elif "accept-encoding" not in existing.lower():
            response.headers["Vary"] = existing + ", Accept-Encoding"

    def __repr__(self) -> str:
        return "<GzipMiddleware min={} level={}>".format(self.minimum_size, self.level)
