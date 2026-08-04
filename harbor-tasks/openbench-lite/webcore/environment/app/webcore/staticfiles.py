"""webcore.staticfiles -- serve files from a directory.

A small handler factory that turns a directory on disk into a route: given a
captured ``path`` parameter it reads the corresponding file, guesses its
``Content-Type``, sets caching/validation headers, and answers ``304 Not
Modified`` for a matching conditional request. It is built entirely on the public
:class:`~webcore.response.Response` API and :mod:`webcore.datetimeutil`.

Security note: :func:`safe_join` refuses to escape the served root (no ``..``
traversal, no absolute paths), so a crafted ``path`` cannot read arbitrary files.

Typical wiring::

    static = StaticFiles("./assets")

    @app.route("/static/{path:path}")
    def serve(request, path):
        return static.serve(request, path)
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from typing import Optional

from .datetimeutil import http_date, is_fresh
from .response import Response
from .status import reason_phrase

__all__ = [
    "StaticFiles",
    "guess_type",
    "safe_join",
    "file_etag",
]

#: A few types the stdlib table sometimes misses or under-specifies.
_EXTRA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".webp": "image/webp",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def guess_type(filename: str) -> str:
    """Guess a ``Content-Type`` for ``filename`` from its extension.

    Consults an override table first (for types the stdlib misreports), then
    :func:`mimetypes.guess_type`, defaulting to ``application/octet-stream``.
    """
    _root, ext = os.path.splitext(filename.lower())
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def safe_join(root: str, requested: str) -> Optional[str]:
    """Join ``requested`` under ``root``, or return ``None`` if it escapes.

    Rejects absolute paths and any ``..`` component that would resolve outside
    ``root``. The returned path is normalised and guaranteed to live within the
    served directory.
    """
    if requested.startswith("/") or requested.startswith("\\"):
        requested = requested.lstrip("/\\")
    # Reject explicit parent traversal early.
    parts = []
    for segment in requested.replace("\\", "/").split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            return None
        parts.append(segment)
    candidate = os.path.normpath(os.path.join(root, *parts))
    root_abs = os.path.abspath(root)
    candidate_abs = os.path.abspath(candidate)
    if candidate_abs != root_abs and not candidate_abs.startswith(root_abs + os.sep):
        return None
    return candidate_abs


def file_etag(path: str, stat_result: Optional[os.stat_result] = None) -> str:
    """Compute a weak-ish ETag from a file's size and mtime.

    Cheap and does not read the file body; two files with the same size and
    modification time hash to the same tag, which is exactly what a validator
    needs for conditional requests.
    """
    st = stat_result or os.stat(path)
    seed = "{}-{}-{}".format(st.st_size, int(st.st_mtime), os.path.basename(path))
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return '"{}"'.format(digest)


class StaticFiles:
    """Serve files from ``directory`` through the :class:`Response` API.

    Parameters
    ----------
    directory:
        The served root. All requests are resolved under it via :func:`safe_join`.
    cache_max_age:
        Seconds for the ``Cache-Control: public, max-age=...`` header.
    index:
        Filename served when a directory itself is requested (default
        ``index.html``); pass ``None`` to disable directory indexes.
    """

    def __init__(self, directory: str, cache_max_age: int = 3600,
                 index: Optional[str] = "index.html") -> None:
        self.directory = os.path.abspath(directory)
        self.cache_max_age = cache_max_age
        self.index = index

    def resolve(self, requested: str) -> Optional[str]:
        """Return the on-disk path for ``requested`` or ``None`` if unsafe/missing."""
        path = safe_join(self.directory, requested)
        if path is None:
            return None
        if os.path.isdir(path) and self.index:
            path = os.path.join(path, self.index)
        if not os.path.isfile(path):
            return None
        return path

    def serve(self, request, requested: str) -> Response:
        """Build the :class:`Response` for ``requested`` (relative to the root).

        Returns 404 for a missing or out-of-root file, 304 when the client's
        ``If-Modified-Since``/``If-None-Match`` still matches, otherwise 200 with
        the body plus ``Content-Type``, ``Content-Length``, ``Last-Modified``,
        ``ETag`` and ``Cache-Control`` headers.
        """
        path = self.resolve(requested)
        if path is None:
            return _text_status(404)

        st = os.stat(path)
        etag = file_etag(path, st)
        last_modified = http_date(st.st_mtime)

        if self._not_modified(request, etag, st.st_mtime):
            resp = Response(304, None, b"")
            resp.headers["ETag"] = etag
            resp.headers["Last-Modified"] = last_modified
            return resp

        with open(path, "rb") as fh:
            body = fh.read()

        resp = Response(200, None, body)
        resp.headers["Content-Type"] = guess_type(path)
        resp.headers["Content-Length"] = str(len(body))
        resp.headers["Last-Modified"] = last_modified
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "public, max-age={}".format(self.cache_max_age)
        if request.method == "HEAD":
            return resp.with_body(b"")
        return resp

    def _not_modified(self, request, etag: str, mtime: float) -> bool:
        if_none_match = request.header("if-none-match")
        if if_none_match and etag in [t.strip() for t in if_none_match.split(",")]:
            return True
        if_modified_since = request.header("if-modified-since")
        if if_modified_since and is_fresh(mtime, if_modified_since):
            return True
        return False

    def handler(self):
        """Return a webcore route handler ``(request, path) -> Response``."""
        def _serve(request, path=""):
            return self.serve(request, path)
        return _serve

    def __repr__(self) -> str:
        return "<StaticFiles {!r}>".format(self.directory)


def _text_status(code: int) -> Response:
    resp = Response(code, None, reason_phrase(code) or str(code))
    resp.headers.setdefault("Content-Type", "text/plain; charset=utf-8")
    return resp
