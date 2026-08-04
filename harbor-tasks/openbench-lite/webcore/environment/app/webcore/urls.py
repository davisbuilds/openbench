"""webcore.urls -- small URL helpers built on top of ``urllib.parse``.

Thin, well-named wrappers so the rest of webcore never has to remember the exact
``urllib.parse`` call site or its keyword arguments: percent-encoding a single
segment vs. a whole path, building a sorted query string, joining a mount prefix
to a route path without doubling or dropping a slash, and splitting a target
into its path and query parts.
"""

from urllib.parse import quote, unquote, urlencode, parse_qsl


#: Characters allowed unescaped inside a single path segment (no ``/``).
_SEGMENT_SAFE = ""
#: Characters allowed unescaped inside a whole path (keeps ``/``).
_PATH_SAFE = "/"


def encode_segment(value):
    """Percent-encode ``value`` for use as one path segment (escapes ``/``)."""
    return quote(str(value), safe=_SEGMENT_SAFE)


def encode_path(value):
    """Percent-encode ``value`` for use as a path (keeps ``/`` unescaped)."""
    return quote(str(value), safe=_PATH_SAFE)


def decode(value):
    """Reverse percent-encoding."""
    return unquote(value)


def build_query(params, sort=True):
    """Render ``params`` (a mapping or pair-iterable) into a query string.

    The result has no leading ``?``. With ``sort=True`` (the default) the pairs
    are ordered by key then value so the output is deterministic.
    """
    if hasattr(params, "items"):
        pairs = list(params.items())
    else:
        pairs = list(params)
    pairs = [(str(k), str(v)) for k, v in pairs]
    if sort:
        pairs.sort()
    return urlencode(pairs)


def split_target(target):
    """Split a request target into ``(path, query_string)``.

    ``"/a/b?x=1"`` -> ``("/a/b", "x=1")``; a target with no query yields an
    empty query string.
    """
    if "?" in target:
        path, query = target.split("?", 1)
        return path, query
    return target, ""


def parse_query(query_string):
    """Parse a query string into a list of ``(key, value)`` pairs (decoded)."""
    return parse_qsl(query_string, keep_blank_values=True)


def join_paths(prefix, path):
    """Join a mount ``prefix`` and a route ``path`` into one path.

    Avoids a doubled slash where they meet and never drops the separator::

        join_paths("/api", "/users")  -> "/api/users"
        join_paths("/api/", "/users") -> "/api/users"
        join_paths("/api", "/")       -> "/api"
        join_paths("/", "/users")     -> "/users"
    """
    if prefix in ("", "/"):
        return path or "/"
    if path in ("", "/"):
        return prefix
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def normalize_path(path):
    """Collapse repeated slashes and guarantee a single leading slash.

    A purely cosmetic normaliser (it does not resolve ``.`` / ``..``); handy for
    tidying a path assembled from user-supplied pieces.
    """
    if not path:
        return "/"
    leading = path.startswith("/")
    trailing = len(path) > 1 and path.endswith("/")
    parts = [segment for segment in path.split("/") if segment]
    joined = "/".join(parts)
    if leading:
        joined = "/" + joined
    if trailing and joined != "/":
        joined = joined + "/"
    return joined or "/"
