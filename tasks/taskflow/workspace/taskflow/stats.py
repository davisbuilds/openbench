"""Small, dependency-free descriptive-statistics helpers.

The orchestration engine collects a lot of small integer series: per-task
attempt counts, run durations in virtual ticks, resource high-water marks,
queue depths sampled per tick. Several of the higher-level modules
(:mod:`taskflow.metrics`, :mod:`taskflow.reporting`, :mod:`taskflow.diagnostics`)
want to summarise those series without pulling in a third-party numerics
package, so this module provides the handful of reductions they need.

Everything here operates on plain sequences of ``int`` or ``float`` and returns
plain Python numbers. The functions are deliberately total: an empty input
never raises, it returns a documented neutral value (usually ``0.0`` or an
empty structure) so callers can summarise a run that produced no data without
sprinkling guards everywhere.

The one slightly opinionated choice is the percentile definition. We use the
*nearest-rank* method on a sorted copy of the data, which is exact, needs no
interpolation, and matches the intuition "the p-th percentile is the smallest
value at or below which at least p percent of the data falls". That keeps the
results stable and integer-friendly for the integer series this engine deals
in.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Number = float


def _as_list(values: Iterable[Number]) -> List[Number]:
    """Materialise ``values`` into a concrete list.

    Most reductions below need to traverse the data more than once (a sum and a
    length, or a sort). Accepting any iterable but immediately realising it into
    a list keeps the public signatures permissive while letting the bodies index
    freely. A list passed in is copied so callers never observe mutation.
    """

    return list(values)


def count(values: Iterable[Number]) -> int:
    """Return the number of items in ``values``."""

    return len(_as_list(values))


def total(values: Iterable[Number]) -> Number:
    """Return the sum of ``values`` (``0`` for an empty series)."""

    return sum(_as_list(values))


def mean(values: Iterable[Number]) -> float:
    """Return the arithmetic mean of ``values``.

    An empty series has no mean; rather than raise, we return ``0.0`` so that a
    per-task average over a run that never executed the task degrades quietly to
    zero. Callers that must distinguish "no data" from "genuine zero" should
    check :func:`count` first.
    """

    data = _as_list(values)
    if not data:
        return 0.0
    return sum(data) / len(data)


def variance(values: Iterable[Number], sample: bool = False) -> float:
    """Return the variance of ``values``.

    Parameters
    ----------
    sample:
        When ``True`` compute the *sample* variance (Bessel's correction,
        dividing by ``n - 1``); when ``False`` (the default) compute the
        *population* variance dividing by ``n``. A series shorter than the
        divisor requires returns ``0.0``.
    """

    data = _as_list(values)
    n = len(data)
    divisor = n - 1 if sample else n
    if divisor <= 0:
        return 0.0
    mu = sum(data) / n
    return sum((x - mu) ** 2 for x in data) / divisor


def stddev(values: Iterable[Number], sample: bool = False) -> float:
    """Return the standard deviation: the square root of :func:`variance`."""

    return math.sqrt(variance(values, sample=sample))


def minimum(values: Iterable[Number], default: Number = 0.0) -> Number:
    """Return the smallest value, or ``default`` for an empty series."""

    data = _as_list(values)
    return min(data) if data else default


def maximum(values: Iterable[Number], default: Number = 0.0) -> Number:
    """Return the largest value, or ``default`` for an empty series."""

    data = _as_list(values)
    return max(data) if data else default


def value_range(values: Iterable[Number]) -> Number:
    """Return ``max - min`` (the spread), or ``0`` for an empty series."""

    data = _as_list(values)
    if not data:
        return 0
    return max(data) - min(data)


def median(values: Iterable[Number]) -> float:
    """Return the median of ``values`` (the 50th percentile, interpolated).

    Unlike :func:`percentile`, the median interpolates between the two middle
    elements of an even-length series, which is the conventional definition and
    the one reporting tables expect for a "typical" figure.
    """

    data = sorted(_as_list(values))
    n = len(data)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return float(data[mid])
    return (data[mid - 1] + data[mid]) / 2.0


def percentile(values: Iterable[Number], pct: float) -> Number:
    """Return the ``pct`` percentile using the nearest-rank method.

    ``pct`` is a number in ``[0, 100]``. The nearest-rank percentile is the
    value at ordinal rank ``ceil(pct/100 * n)`` in the sorted series (1-based),
    clamped into range. This is exact and interpolation-free, so for an integer
    series it returns an actual observed integer. ``percentile(xs, 100)`` is the
    maximum and ``percentile(xs, 0)`` the minimum.
    """

    if not 0.0 <= pct <= 100.0:
        raise ValueError("percentile pct must be in [0, 100], got {}".format(pct))
    data = sorted(_as_list(values))
    n = len(data)
    if n == 0:
        return 0
    if pct == 0.0:
        return data[0]
    rank = math.ceil(pct / 100.0 * n)
    index = min(max(rank, 1), n) - 1
    return data[index]


def quantiles(values: Iterable[Number], n: int = 4) -> List[Number]:
    """Return the ``n - 1`` cut points dividing ``values`` into ``n`` groups.

    With ``n == 4`` this yields the three quartile boundaries (the 25th, 50th
    and 75th percentiles). The cut points are computed with the same nearest
    rank rule as :func:`percentile` so they are drawn from the observed data.
    """

    if n < 2:
        raise ValueError("quantiles requires n >= 2, got {}".format(n))
    return [percentile(values, 100.0 * k / n) for k in range(1, n)]


def mode(values: Iterable[Number]) -> Optional[Number]:
    """Return the most frequent value, or ``None`` for an empty series.

    Ties are broken by first appearance, so the result is deterministic for the
    ordered integer series this engine produces.
    """

    data = _as_list(values)
    if not data:
        return None
    counts: Dict[Number, int] = {}
    order: List[Number] = []
    for value in data:
        if value not in counts:
            counts[value] = 0
            order.append(value)
        counts[value] += 1
    best = order[0]
    for value in order:
        if counts[value] > counts[best]:
            best = value
    return best


def frequency_table(values: Iterable[Number]) -> Dict[Number, int]:
    """Return a ``{value: occurrences}`` count, keyed in first-seen order."""

    table: Dict[Number, int] = {}
    for value in _as_list(values):
        table[value] = table.get(value, 0) + 1
    return table


def cumulative_sum(values: Iterable[Number]) -> List[Number]:
    """Return the running totals of ``values`` (same length as the input)."""

    running: Number = 0
    out: List[Number] = []
    for value in _as_list(values):
        running += value
        out.append(running)
    return out


def normalise(values: Iterable[Number]) -> List[float]:
    """Scale ``values`` so they sum to ``1.0`` (a probability distribution).

    A series that sums to zero (all zeros, or empty) is returned as all zeros of
    the same length, because there is no meaningful way to normalise it.
    """

    data = _as_list(values)
    s = sum(data)
    if s == 0:
        return [0.0 for _ in data]
    return [value / s for value in data]


def clamp(value: Number, low: Number, high: Number) -> Number:
    """Return ``value`` confined to the closed interval ``[low, high]``."""

    if low > high:
        raise ValueError("clamp low {} exceeds high {}".format(low, high))
    if value < low:
        return low
    if value > high:
        return high
    return value


def histogram(
    values: Iterable[Number], bins: int = 10
) -> List[Tuple[float, float, int]]:
    """Bucket ``values`` into ``bins`` equal-width bins over their range.

    Returns a list of ``(low, high, count)`` triples covering ``[min, max]``.
    The final bin is closed on the right so the maximum value lands in it rather
    than falling off the end. An empty series returns an empty list; a series
    with zero spread (all equal) returns a single bin holding everything.
    """

    if bins < 1:
        raise ValueError("histogram requires bins >= 1, got {}".format(bins))
    data = _as_list(values)
    if not data:
        return []
    low = min(data)
    high = max(data)
    if low == high:
        return [(float(low), float(high), len(data))]
    width = (high - low) / bins
    edges = [low + width * i for i in range(bins + 1)]
    counts = [0] * bins
    for value in data:
        if value >= high:
            counts[-1] += 1
            continue
        index = int((value - low) / width)
        index = min(max(index, 0), bins - 1)
        counts[index] += 1
    return [(edges[i], edges[i + 1], counts[i]) for i in range(bins)]


class RunningStats:
    """An online accumulator for a stream of numbers.

    Where the free functions above take a whole series at once, ``RunningStats``
    folds values in one at a time, which is what a metric sampler wants when it
    observes durations tick by tick. It keeps a running count, sum, min, max and
    the running mean/variance via Welford's numerically stable algorithm, so it
    never has to retain the individual samples.
    """

    __slots__ = ("_count", "_sum", "_min", "_max", "_mean", "_m2")

    def __init__(self) -> None:
        self._count = 0
        self._sum: Number = 0
        self._min: Optional[Number] = None
        self._max: Optional[Number] = None
        self._mean = 0.0
        self._m2 = 0.0

    def add(self, value: Number) -> "RunningStats":
        """Fold ``value`` into the accumulator and return ``self`` for chaining."""

        self._count += 1
        self._sum += value
        if self._min is None or value < self._min:
            self._min = value
        if self._max is None or value > self._max:
            self._max = value
        delta = value - self._mean
        self._mean += delta / self._count
        self._m2 += delta * (value - self._mean)
        return self

    def extend(self, values: Iterable[Number]) -> "RunningStats":
        """Fold every value of ``values`` in, in order."""

        for value in values:
            self.add(value)
        return self

    @property
    def count(self) -> int:
        """The number of values folded in so far."""

        return self._count

    @property
    def total(self) -> Number:
        """The running sum of every value seen."""

        return self._sum

    @property
    def mean(self) -> float:
        """The running arithmetic mean (``0.0`` before any value is added)."""

        return self._mean if self._count else 0.0

    @property
    def minimum(self) -> Number:
        """The smallest value seen (``0`` before any value is added)."""

        return self._min if self._min is not None else 0

    @property
    def maximum(self) -> Number:
        """The largest value seen (``0`` before any value is added)."""

        return self._max if self._max is not None else 0

    def variance(self, sample: bool = False) -> float:
        """Return the running population (or sample) variance."""

        divisor = self._count - 1 if sample else self._count
        if divisor <= 0:
            return 0.0
        return self._m2 / divisor

    def stddev(self, sample: bool = False) -> float:
        """Return the running standard deviation."""

        return math.sqrt(self.variance(sample=sample))

    def snapshot(self) -> Dict[str, Number]:
        """Return a plain-dict view of the accumulated statistics."""

        return {
            "count": self._count,
            "sum": self._sum,
            "min": self.minimum,
            "max": self.maximum,
            "mean": self.mean,
            "stddev": self.stddev(),
        }

    def __repr__(self) -> str:
        return "RunningStats(count={}, mean={:.3f})".format(
            self._count, self.mean
        )
