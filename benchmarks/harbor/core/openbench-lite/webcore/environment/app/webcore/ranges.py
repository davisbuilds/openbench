"""webcore.ranges -- HTTP Range request handling.

Byte-range requests let a client fetch part of a representation -- resuming a
download, seeking in media -- via the ``Range`` header, answered with ``206
Partial Content`` and a ``Content-Range``. This module parses the (single-range
subset of the) ``Range`` grammar, clamps it against a body length, and builds the
partial response headers, all on top of the public
:class:`~webcore.response.Response` API.

Multi-range (``multipart/byteranges``) is intentionally out of scope; a
multi-range request degrades to the full ``200`` response, which is a compliant
choice for a server that does not implement it.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .response import Response

__all__ = [
    "ByteRange",
    "parse_range",
    "apply_range",
    "content_range_header",
]


class ByteRange:
    """A resolved ``[start, end]`` inclusive byte interval within a body.

    Both bounds are concrete indices into a body of known length; use
    :meth:`length` for the number of bytes covered.
    """

    __slots__ = ("start", "end")

    def __init__(self, start: int, end: int) -> None:
        self.start = start
        self.end = end

    def length(self) -> int:
        """Number of bytes in the (inclusive) range."""
        return self.end - self.start + 1

    def slice(self, body: bytes) -> bytes:
        """Extract this range from ``body`` (end is inclusive)."""
        return body[self.start:self.end + 1]

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ByteRange)
            and (self.start, self.end) == (other.start, other.end)
        )

    def __repr__(self) -> str:
        return "ByteRange({}, {})".format(self.start, self.end)


def _parse_single(spec: str, total: int) -> Optional[ByteRange]:
    """Resolve one ``start-end`` / ``start-`` / ``-suffix`` spec against ``total``."""
    spec = spec.strip()
    if "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if start_s == "":
            # Suffix range: last N bytes.
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, total - suffix)
            end = total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
    except ValueError:
        return None
    if start > end or start >= total:
        return None
    end = min(end, total - 1)
    return ByteRange(start, end)


def parse_range(header: Optional[str], total: int) -> Optional[ByteRange]:
    """Parse a ``Range`` header for a body of ``total`` bytes.

    Returns a resolved :class:`ByteRange`, or ``None`` when the header is absent,
    not ``bytes=``, unsatisfiable, or specifies multiple ranges (unsupported).
    ``None`` signals "serve the whole thing" to the caller.
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[len("bytes="):]
    if "," in spec:
        return None  # multi-range unsupported -> full response
    return _parse_single(spec, total)


def content_range_header(byte_range: ByteRange, total: int) -> str:
    """Build the ``Content-Range`` header value for a satisfied range."""
    return "bytes {}-{}/{}".format(byte_range.start, byte_range.end, total)


def unsatisfiable_response(total: int) -> Response:
    """Build a ``416 Range Not Satisfiable`` response with ``Content-Range``."""
    resp = Response(416, None, b"")
    resp.headers["Content-Range"] = "bytes */{}".format(total)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    return resp


def apply_range(request, body: bytes, content_type: str = "application/octet-stream",
                status: int = 200) -> Response:
    """Serve ``body`` honouring a ``Range`` header if present.

    Returns a full ``200`` when there is no (valid) range, a ``206 Partial
    Content`` with ``Content-Range`` when a range is satisfied, or a ``416`` when
    a ``bytes=`` range is syntactically valid but wholly unsatisfiable. Every
    response advertises ``Accept-Ranges: bytes``.
    """
    total = len(body)
    header = request.header("range")
    byte_range = parse_range(header, total)

    if byte_range is None:
        if header and header.strip().lower().startswith("bytes=") and total > 0:
            return unsatisfiable_response(total)
        resp = Response(status, None, body)
        resp.headers["Content-Type"] = content_type
        resp.headers["Content-Length"] = str(total)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp

    chunk = byte_range.slice(body)
    resp = Response(206, None, chunk)
    resp.headers["Content-Type"] = content_type
    resp.headers["Content-Length"] = str(len(chunk))
    resp.headers["Content-Range"] = content_range_header(byte_range, total)
    resp.headers["Accept-Ranges"] = "bytes"
    return resp
