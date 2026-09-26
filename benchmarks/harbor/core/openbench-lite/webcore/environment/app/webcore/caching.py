"""webcore.caching -- an in-memory response cache.

Handlers that are expensive but rarely change can be wrapped so their
:class:`~webcore.response.Response` is remembered for a while. This module
provides:

*   :class:`ResponseCache` -- a bounded, TTL + LRU key/response store.
*   :func:`cached` -- a decorator that memoises a handler's response by a cache
    key derived from the request (method + full path by default).
*   :class:`CacheControl` -- a tiny builder for the ``Cache-Control`` header.

Everything is process-local and unsynchronised -- appropriate for an in-process
framework, not a shared cache tier. Only successful (2xx) responses are cached
by default so errors are not pinned.
"""

from __future__ import annotations

import functools
import time
from collections import OrderedDict
from typing import Any, Callable, List, Optional, Tuple

from .response import Response
from .status import is_success

__all__ = [
    "ResponseCache",
    "CacheEntry",
    "CacheControl",
    "cached",
    "default_key",
]


def default_key(request) -> str:
    """Derive a cache key from a request: ``"METHOD full/path?query"``."""
    return "{} {}".format(request.method, request.full_path)


class CacheEntry:
    """One stored response plus the wall-clock time it expires."""

    __slots__ = ("response", "expires_at", "created_at")

    def __init__(self, response: Response, ttl: float, now: float) -> None:
        self.response = response
        self.created_at = now
        self.expires_at = now + ttl if ttl > 0 else float("inf")

    def is_fresh(self, now: float) -> bool:
        """True while the entry has not passed its expiry time."""
        return now < self.expires_at

    def age(self, now: float) -> float:
        """Seconds since the entry was stored."""
        return now - self.created_at


class ResponseCache:
    """A bounded TTL + LRU store mapping keys to responses.

    Parameters
    ----------
    max_size:
        Maximum number of entries; the least-recently-used entry is evicted when
        the cache is full.
    ttl:
        Default lifetime in seconds for stored entries (``0`` = no expiry).
    time_func:
        Clock source, injectable for deterministic tests.
    """

    def __init__(self, max_size: int = 128, ttl: float = 60.0,
                 time_func: Callable[[], float] = time.time) -> None:
        self.max_size = max_size
        self.ttl = ttl
        self._time = time_func
        self._store: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Response]:
        """Return a fresh cached response for ``key`` or ``None`` (a miss).

        A hit moves the entry to the most-recently-used end; an expired entry is
        evicted and counts as a miss.
        """
        now = self._time()
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        if not entry.is_fresh(now):
            del self._store[key]
            self.misses += 1
            return None
        self._store.move_to_end(key)
        self.hits += 1
        return entry.response

    def set(self, key: str, response: Response, ttl: Optional[float] = None) -> None:
        """Store ``response`` under ``key`` with an optional per-entry ``ttl``."""
        now = self._time()
        effective_ttl = self.ttl if ttl is None else ttl
        self._store[key] = CacheEntry(response, effective_ttl, now)
        self._store.move_to_end(key)
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)  # drop least-recently-used

    def invalidate(self, key: str) -> bool:
        """Drop ``key`` if present; return whether anything was removed."""
        return self._store.pop(key, None) is not None

    def clear(self) -> None:
        """Empty the cache and reset hit/miss counters."""
        self._store.clear()
        self.hits = 0
        self.misses = 0

    def purge_expired(self) -> int:
        """Evict every stale entry now; return the count removed."""
        now = self._time()
        stale = [k for k, entry in self._store.items() if not entry.is_fresh(now)]
        for key in stale:
            del self._store[key]
        return len(stale)

    def keys(self) -> List[str]:
        """The currently stored keys (LRU order, oldest first)."""
        return list(self._store.keys())

    @property
    def hit_ratio(self) -> float:
        """Hits divided by total lookups (0.0 when no lookups yet)."""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict:
        """A snapshot of size and hit/miss counters for introspection."""
        return {
            "size": len(self._store),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_ratio": round(self.hit_ratio, 4),
        }

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: str) -> bool:
        entry = self._store.get(key)
        return entry is not None and entry.is_fresh(self._time())

    def __repr__(self) -> str:
        return "<ResponseCache {}/{}>".format(len(self._store), self.max_size)


def cached(ttl: float = 60.0, cache: Optional[ResponseCache] = None,
           key_func: Callable[[Any], str] = default_key,
           cache_predicate: Optional[Callable[[Response], bool]] = None) -> Callable:
    """Decorate a handler so its response is memoised per cache key.

    Parameters
    ----------
    ttl:
        Freshness window in seconds for stored responses.
    cache:
        A shared :class:`ResponseCache`; a private one is created if omitted.
    key_func:
        Maps a request to a string key (default: method + full path).
    cache_predicate:
        Decides whether a produced response is cacheable; defaults to "2xx only".

    The wrapped handler keeps the ``(request, **params)`` signature. The bound
    cache is exposed on the wrapper as ``.cache`` for inspection or invalidation.
    """
    store = cache if cache is not None else ResponseCache(ttl=ttl)
    should_cache = cache_predicate or (lambda resp: is_success(resp.status))

    def decorator(handler: Callable) -> Callable:
        @functools.wraps(handler)
        def wrapper(request, **params):
            key = key_func(request)
            hit = store.get(key)
            if hit is not None:
                return _mark_hit(hit)
            response = handler(request, **params)
            if isinstance(response, Response) and should_cache(response):
                store.set(key, response, ttl=ttl)
            return response

        wrapper.cache = store  # type: ignore[attr-defined]
        return wrapper

    return decorator


def _mark_hit(response: Response) -> Response:
    """Annotate a cache-hit response with an ``X-Cache: HIT`` header (copied)."""
    clone = response.with_body(response.body)
    clone.headers["X-Cache"] = "HIT"
    return clone


class CacheControl:
    """A small builder for a ``Cache-Control`` header value.

    ::

        CacheControl().public().max_age(300).build()   # "public, max-age=300"
    """

    def __init__(self) -> None:
        self._directives: List[Tuple[str, Optional[str]]] = []

    def public(self) -> "CacheControl":
        self._directives.append(("public", None))
        return self

    def private(self) -> "CacheControl":
        self._directives.append(("private", None))
        return self

    def no_store(self) -> "CacheControl":
        self._directives.append(("no-store", None))
        return self

    def no_cache(self) -> "CacheControl":
        self._directives.append(("no-cache", None))
        return self

    def max_age(self, seconds: int) -> "CacheControl":
        self._directives.append(("max-age", str(int(seconds))))
        return self

    def s_maxage(self, seconds: int) -> "CacheControl":
        self._directives.append(("s-maxage", str(int(seconds))))
        return self

    def must_revalidate(self) -> "CacheControl":
        self._directives.append(("must-revalidate", None))
        return self

    def build(self) -> str:
        """Render the accumulated directives into a header value string."""
        parts = []
        for name, value in self._directives:
            parts.append(name if value is None else "{}={}".format(name, value))
        return ", ".join(parts)

    def apply(self, response: Response) -> Response:
        """Set the built value as ``response``'s ``Cache-Control`` header."""
        response.headers["Cache-Control"] = self.build()
        return response

    def __str__(self) -> str:
        return self.build()
