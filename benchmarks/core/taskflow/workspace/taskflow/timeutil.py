"""Virtual-clock helpers and backoff-curve utilities.

The scheduler runs on an integer *virtual* clock: every tick is one unit, and
no wall-clock time, sleeping, or randomness is ever involved. This module
collects the small time-shaped helpers that several layers want but that do not
belong inside the scheduler's hot loop:

* :class:`VirtualClock` -- a tiny mutable counter with a checkpoint/elapsed API,
  handy for tests and for tools that want to simulate the passage of virtual
  time without driving a whole scheduler.
* :class:`Stopwatch` -- measure spans against a clock and accumulate laps.
* the ``backoff_*`` functions and :func:`backoff_curve` -- pure functions that
  reproduce and extend the delay maths in :mod:`taskflow.retry`, so planning and
  reporting tools can show a retry schedule *without* constructing a
  ``RetryPolicy`` or mutating one.

Nothing here imports the scheduler or the retry policy; it is deliberately
self-contained value logic so it can be reused (and unit-reasoned about) in
isolation. All delays are integers of virtual ticks unless a float cap forces a
fractional result.
"""

from __future__ import annotations

from typing import Callable, Iterator, List, Optional


class VirtualClock:
    """A monotonic, integer virtual clock.

    The clock starts at ``start`` (default 0) and only ever moves forward via
    :meth:`advance` or :meth:`set`. It carries no real time; it is simply a
    counter with a small vocabulary that reads naturally at call sites that
    reason about virtual time.
    """

    __slots__ = ("_now", "_start")

    def __init__(self, start: int = 0) -> None:
        if start < 0:
            raise ValueError("virtual clock cannot start before 0")
        self._start = start
        self._now = start

    @property
    def now(self) -> int:
        """The current virtual time."""

        return self._now

    def advance(self, ticks: int = 1) -> int:
        """Move the clock forward by ``ticks`` and return the new time.

        Advancing by a negative amount is rejected: the virtual clock is
        monotonic, mirroring the scheduler which never rewinds.
        """

        if ticks < 0:
            raise ValueError("cannot advance a virtual clock backwards")
        self._now += ticks
        return self._now

    def set(self, when: int) -> int:
        """Jump the clock to ``when`` (which must not be in the past)."""

        if when < self._now:
            raise ValueError(
                "cannot set virtual clock back from {} to {}".format(
                    self._now, when
                )
            )
        self._now = when
        return self._now

    def elapsed(self) -> int:
        """Return how far the clock has moved since it was created."""

        return self._now - self._start

    def reset(self) -> None:
        """Return the clock to its starting time."""

        self._now = self._start

    def tick_iter(self, count: int) -> Iterator[int]:
        """Yield ``count`` successive tick values, advancing the clock each time.

        The first value yielded is the current time *before* advancing, so
        ``list(clock.tick_iter(3))`` starting at 0 yields ``[0, 1, 2]`` and
        leaves the clock at 3.
        """

        if count < 0:
            raise ValueError("tick_iter count must be non-negative")
        for _ in range(count):
            value = self._now
            self._now += 1
            yield value

    def __int__(self) -> int:
        return self._now

    def __repr__(self) -> str:
        return "VirtualClock(now={}, start={})".format(self._now, self._start)


class Stopwatch:
    """Measure virtual-time spans against a :class:`VirtualClock`.

    A stopwatch is started at some virtual time and can be sampled for how much
    time has elapsed since then, or lapped to record a span and begin a fresh
    one. It reads the clock rather than owning time, so several stopwatches can
    watch the same clock and measure overlapping spans.
    """

    __slots__ = ("_clock", "_started_at", "_laps")

    def __init__(self, clock: VirtualClock) -> None:
        self._clock = clock
        self._started_at = clock.now
        self._laps: List[int] = []

    def restart(self) -> None:
        """Reset the origin to the clock's current time and clear laps."""

        self._started_at = self._clock.now
        self._laps = []

    def elapsed(self) -> int:
        """Return the ticks elapsed since the last start/lap origin."""

        return self._clock.now - self._started_at

    def lap(self) -> int:
        """Record the current span, reset the origin, and return the span."""

        span = self.elapsed()
        self._laps.append(span)
        self._started_at = self._clock.now
        return span

    def laps(self) -> List[int]:
        """Return the recorded lap spans, in order."""

        return list(self._laps)

    def total(self) -> int:
        """Return the summed length of every recorded lap."""

        return sum(self._laps)

    def __repr__(self) -> str:
        return "Stopwatch(elapsed={}, laps={})".format(
            self.elapsed(), len(self._laps)
        )


def backoff_constant(base: int, attempt: int) -> int:
    """Return the constant backoff delay: always ``base`` regardless of attempt.

    ``attempt`` is the 1-based number of the failed attempt. It is accepted for a
    uniform signature with :func:`backoff_exponential` but does not affect the
    result, matching the constant strategy in :mod:`taskflow.retry`.
    """

    _ = attempt
    if base < 0:
        raise ValueError("backoff base must be non-negative")
    return base


def backoff_exponential(
    base: int, attempt: int, factor: int = 2, cap: Optional[int] = None
) -> int:
    """Return ``base * factor ** (attempt - 1)``, optionally capped.

    This reproduces the exponential curve :class:`taskflow.retry.RetryPolicy`
    computes, exposed as a free function so planning and reporting tools can
    project a retry schedule ahead of time. ``attempt`` below 1 is treated as 1.
    """

    if base < 0:
        raise ValueError("backoff base must be non-negative")
    if factor < 1:
        raise ValueError("backoff factor must be >= 1")
    step = max(attempt, 1) - 1
    delay = base * (factor ** step)
    if cap is not None and delay > cap:
        return cap
    return delay


def backoff_linear(base: int, attempt: int, cap: Optional[int] = None) -> int:
    """Return a linearly growing delay: ``base * attempt``, optionally capped.

    A middle ground between constant and exponential that some pipelines prefer:
    the delay grows by ``base`` every attempt rather than multiplying.
    """

    if base < 0:
        raise ValueError("backoff base must be non-negative")
    delay = base * max(attempt, 1)
    if cap is not None and delay > cap:
        return cap
    return delay


def backoff_curve(
    strategy: str,
    base: int,
    attempts: int,
    factor: int = 2,
    cap: Optional[int] = None,
) -> List[int]:
    """Return the successive delays a policy would impose over ``attempts`` tries.

    Produces the list of delays applied *between* attempts, so a policy allowing
    ``attempts`` total tries yields ``attempts - 1`` delays (there is no delay
    after the final, giving-up attempt). ``strategy`` is one of ``"constant"``,
    ``"linear"`` or ``"exponential"``.

    Example::

        backoff_curve("exponential", base=1, attempts=4, factor=2)
        # -> [1, 2, 4]
    """

    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    fn = _resolve_strategy(strategy, factor=factor, cap=cap)
    return [fn(base, n) for n in range(1, attempts)]


def cumulative_backoff(
    strategy: str,
    base: int,
    attempts: int,
    factor: int = 2,
    cap: Optional[int] = None,
) -> int:
    """Return the total virtual time spent waiting across every retry.

    The sum of :func:`backoff_curve` -- i.e. how much extra virtual time a fully
    exhausted retry sequence adds beyond the execution time itself. Useful when
    a planner wants to bound the worst-case makespan of a flaky task.
    """

    return sum(backoff_curve(strategy, base, attempts, factor=factor, cap=cap))


def _resolve_strategy(
    strategy: str, factor: int, cap: Optional[int]
) -> Callable[[int, int], int]:
    """Return a ``(base, attempt) -> delay`` function for ``strategy``."""

    if strategy == "constant":
        return lambda base, attempt: backoff_constant(base, attempt)
    if strategy == "linear":
        return lambda base, attempt: backoff_linear(base, attempt, cap=cap)
    if strategy == "exponential":
        return lambda base, attempt: backoff_exponential(
            base, attempt, factor=factor, cap=cap
        )
    raise ValueError("unknown backoff strategy: {!r}".format(strategy))


def format_ticks(ticks: int) -> str:
    """Render a virtual-tick count as a compact human string.

    Purely cosmetic, for reporting. Virtual ticks have no unit, so this simply
    pluralises: ``format_ticks(1) == "1 tick"`` and ``format_ticks(5) ==
    "5 ticks"``.
    """

    return "{} tick{}".format(ticks, "" if ticks == 1 else "s")
