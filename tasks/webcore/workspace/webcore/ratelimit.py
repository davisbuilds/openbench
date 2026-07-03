"""webcore.ratelimit -- in-process rate limiting.

A token-bucket limiter and the middleware that applies it per client key. Each
key (by default the request path, but commonly a client identifier) owns a
bucket that refills at a steady rate up to a burst capacity; a request consumes
one token, and an empty bucket yields ``429 Too Many Requests`` with a
``Retry-After`` header.

The store is a plain in-memory dict -- correct for a single process, not a
distributed limiter. The clock is injectable so behaviour is deterministic in
tests.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from .exceptions import TooManyRequests

__all__ = ["TokenBucket", "RateLimiter", "RateLimitMiddleware"]


class TokenBucket:
    """A classic token bucket.

    Parameters
    ----------
    capacity:
        Maximum tokens (the burst size).
    refill_rate:
        Tokens added per second.
    time_func:
        Clock source.

    Tokens accrue continuously; :meth:`consume` deducts ``cost`` if available and
    reports success, otherwise leaves the bucket unchanged.
    """

    __slots__ = ("capacity", "refill_rate", "_tokens", "_last", "_time")

    def __init__(self, capacity: float, refill_rate: float,
                 time_func: Callable[[], float] = time.monotonic) -> None:
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._tokens = float(capacity)
        self._time = time_func
        self._last = time_func()

    def _refill(self) -> None:
        now = self._time()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
            self._last = now

    def consume(self, cost: float = 1.0) -> bool:
        """Try to remove ``cost`` tokens; return whether it succeeded."""
        self._refill()
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    @property
    def tokens(self) -> float:
        """The current (refilled) token count."""
        self._refill()
        return self._tokens

    def retry_after(self, cost: float = 1.0) -> float:
        """Seconds until ``cost`` tokens will be available (0 if already)."""
        self._refill()
        if self._tokens >= cost:
            return 0.0
        if self.refill_rate <= 0:
            return float("inf")
        return (cost - self._tokens) / self.refill_rate


class RateLimiter:
    """A registry of per-key :class:`TokenBucket` limiters."""

    def __init__(self, capacity: float = 60, refill_rate: float = 1.0,
                 time_func: Callable[[], float] = time.monotonic) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._time = time_func
        self._buckets: Dict[str, TokenBucket] = {}

    def bucket_for(self, key: str) -> TokenBucket:
        """Return (creating if needed) the bucket for ``key``."""
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self.capacity, self.refill_rate, self._time)
            self._buckets[key] = bucket
        return bucket

    def allow(self, key: str, cost: float = 1.0) -> bool:
        """Whether a request keyed ``key`` may proceed right now."""
        return self.bucket_for(key).consume(cost)

    def retry_after(self, key: str, cost: float = 1.0) -> float:
        """Seconds until ``key`` regains capacity for ``cost`` tokens."""
        return self.bucket_for(key).retry_after(cost)

    def reset(self, key: Optional[str] = None) -> None:
        """Drop one key's bucket, or all of them when ``key`` is ``None``."""
        if key is None:
            self._buckets.clear()
        else:
            self._buckets.pop(key, None)

    def __len__(self) -> int:
        return len(self._buckets)


class RateLimitMiddleware:
    """Middleware enforcing a :class:`RateLimiter` per request key.

    Parameters
    ----------
    capacity / refill_rate:
        Bucket size and per-second refill for each key.
    key_func:
        Maps a request to a limiter key (default: the request path). Override to
        key on a client id, token, or forwarded address.

    A denied request raises :class:`~webcore.exceptions.TooManyRequests` with a
    ``Retry-After`` header (whole seconds, rounded up).
    """

    def __init__(self, capacity: float = 60, refill_rate: float = 1.0,
                 key_func: Optional[Callable[[Any], str]] = None,
                 time_func: Callable[[], float] = time.monotonic) -> None:
        self.limiter = RateLimiter(capacity, refill_rate, time_func)
        self.key_func = key_func or (lambda request: request.path)

    def __call__(self, request, next_call):
        key = self.key_func(request)
        if self.limiter.allow(key):
            return next_call(request)
        retry = self.limiter.retry_after(key)
        headers = {}
        if retry != float("inf"):
            import math
            headers["Retry-After"] = str(int(math.ceil(retry)))
        raise TooManyRequests(description="rate limit exceeded", headers=headers)

    def __repr__(self) -> str:
        return "<RateLimitMiddleware cap={} rate={}>".format(
            self.limiter.capacity, self.limiter.refill_rate
        )
