"""Retry policies for failed job attempts.

A :class:`RetryPolicy` answers two questions the scheduler asks whenever an
attempt fails:

1. *Should we try again?* -- :meth:`RetryPolicy.should_retry`, given how many
   attempts have already been made.
2. *If so, how long until the next attempt?* -- :meth:`RetryPolicy.next_delay`,
   a virtual-time delay derived from the configured backoff strategy.

Both are pure functions of the attempt count and the policy configuration, so
the scheduler stays fully deterministic.
"""

from __future__ import annotations

from taskflow.model import ConfigError

CONSTANT = "constant"
EXPONENTIAL = "exponential"

_BACKOFF_KINDS = frozenset({CONSTANT, EXPONENTIAL})


class RetryPolicy:
    """Decide whether and when a failed attempt should be retried.

    Parameters
    ----------
    max_attempts:
        The total number of attempts permitted, counting the first one. A
        value of ``1`` means "no retries": the first failure is permanent. Must
        be a positive integer.
    backoff:
        Either ``"constant"`` or ``"exponential"``. Constant backoff waits the
        same ``base`` delay before every retry. Exponential backoff waits
        ``base * factor ** (attempts_made - 1)``.
    base:
        The base virtual-time delay applied before a retry. Must be
        non-negative.
    factor:
        The growth factor used only by exponential backoff. Must be >= 1.
    max_delay:
        Optional cap applied to the computed delay so exponential growth cannot
        run away.
    """

    __slots__ = ("max_attempts", "backoff", "base", "factor", "max_delay")

    def __init__(
        self,
        max_attempts=1,
        backoff=CONSTANT,
        base=0,
        factor=2,
        max_delay=None,
    ):
        self.max_attempts = int(max_attempts)
        if self.max_attempts < 1:
            raise ConfigError("max_attempts must be >= 1")
        if backoff not in _BACKOFF_KINDS:
            raise ConfigError(
                "unknown backoff strategy: {!r} (expected one of {})".format(
                    backoff, sorted(_BACKOFF_KINDS)
                )
            )
        self.backoff = backoff
        self.base = int(base)
        if self.base < 0:
            raise ConfigError("backoff base must be >= 0")
        self.factor = int(factor)
        if self.factor < 1:
            raise ConfigError("backoff factor must be >= 1")
        self.max_delay = None if max_delay is None else int(max_delay)
        if self.max_delay is not None and self.max_delay < 0:
            raise ConfigError("max_delay must be >= 0")

    def attempts_remaining(self, attempts_made):
        """Return how many attempts are still permitted after ``attempts_made``.

        ``attempts_made`` is the number of attempts already completed (so it is
        ``1`` immediately after the first attempt fails). The result is clamped
        at zero and never goes negative.
        """

        remaining = self.max_attempts - attempts_made
        return remaining if remaining > 0 else 0

    def should_retry(self, attempts_made):
        """Return ``True`` if another attempt is permitted.

        Given ``attempts_made`` completed attempts, a retry is allowed while
        the run has not yet exhausted its budget -- that is, while at least one
        attempt still remains. With ``max_attempts == 3`` this yields
        ``should_retry(1) is True``, ``should_retry(2) is True`` and
        ``should_retry(3) is False``: the third attempt is the last one, after
        which the failure becomes permanent.
        """

        return attempts_made < self.max_attempts - 1

    def is_permanent_failure(self, attempts_made):
        """Return ``True`` if a failure after ``attempts_made`` attempts is final."""

        return not self.should_retry(attempts_made)

    def next_delay(self, attempts_made):
        """Return the virtual-time delay before the next attempt.

        For constant backoff this is always ``base``. For exponential backoff
        it is ``base * factor ** (attempts_made - 1)`` -- so the delay after the
        first failure is ``base``, after the second ``base * factor``, and so
        on -- capped at ``max_delay`` when configured.
        """

        if attempts_made < 1:
            attempts_made = 1
        if self.backoff == CONSTANT:
            delay = self.base
        else:
            delay = self.base * (self.factor ** (attempts_made - 1))
        if self.max_delay is not None and delay > self.max_delay:
            delay = self.max_delay
        return delay

    def describe(self):
        """Return a human-readable one-line description of the policy."""

        if self.backoff == CONSTANT:
            shape = "constant {}".format(self.base)
        else:
            shape = "exponential base={} factor={}".format(
                self.base, self.factor
            )
        return "RetryPolicy(max_attempts={}, backoff={})".format(
            self.max_attempts, shape
        )

    def to_dict(self):
        """Return the policy as a plain dictionary (round-trips through config)."""

        data = {
            "max_attempts": self.max_attempts,
            "backoff": self.backoff,
            "base": self.base,
            "factor": self.factor,
        }
        if self.max_delay is not None:
            data["max_delay"] = self.max_delay
        return data

    @classmethod
    def from_dict(cls, data):
        """Build a policy from a plain dictionary, ignoring unknown keys."""

        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError("retry policy must be a mapping")
        known = ("max_attempts", "backoff", "base", "factor", "max_delay")
        kwargs = {k: data[k] for k in known if k in data}
        return cls(**kwargs)

    def __eq__(self, other):
        if not isinstance(other, RetryPolicy):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self):
        return self.describe()
