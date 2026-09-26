"""Metric primitives and an event-stream collector.

:class:`taskflow.history.History` answers structural questions about a run (what
ran, in what order, what failed). Metrics answer *quantitative* ones: how many
attempts in total, how long jobs spent running, how deep the ready queue got,
what the peak resource usage was. This module provides the small metric
primitives to express those figures and a :class:`MetricsCollector` that folds
them out of the same :class:`~taskflow.events.EventBus` the scheduler already
narrates on -- so metrics cost nothing at the call site and stay perfectly in
step with the run.

The primitives:

* :class:`Counter` -- a monotonically increasing tally.
* :class:`Gauge` -- a value that goes up and down, remembering its peak.
* :class:`Histogram` -- a distribution of observed values, summarised via
  :mod:`taskflow.stats`.
* :class:`Timer` -- start/stop spans on the virtual clock, feeding a histogram
  of durations.

They live in a :class:`MetricRegistry` that hands them out by name (creating on
first use) so different observers can share one namespace. Everything is
deterministic and virtual-time based; no wall clock is ever read.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from taskflow import stats


class Counter:
    """A monotonically increasing integer tally.

    Counters only go up. They are the right primitive for "how many times did X
    happen": attempts started, jobs failed, resources acquired. Incrementing by a
    negative amount is rejected so the monotonic invariant cannot be violated by
    accident.
    """

    __slots__ = ("name", "_value")

    def __init__(self, name: str) -> None:
        self.name = name
        self._value = 0

    def increment(self, amount: int = 1) -> int:
        """Add ``amount`` (default 1) to the counter and return the new value."""

        if amount < 0:
            raise ValueError("counters cannot decrease")
        self._value += amount
        return self._value

    @property
    def value(self) -> int:
        """The current count."""

        return self._value

    def reset(self) -> None:
        """Return the counter to zero."""

        self._value = 0

    def __repr__(self) -> str:
        return "Counter({!r}={})".format(self.name, self._value)


class Gauge:
    """A value that can rise and fall, tracking its high-water mark.

    Where a counter only climbs, a gauge measures a level that fluctuates -- the
    number of jobs currently running, the depth of the ready queue, the units
    held in a resource pool. It remembers the maximum value it ever held so a
    report can show how close the run came to a limit.
    """

    __slots__ = ("name", "_value", "_peak")

    def __init__(self, name: str) -> None:
        self.name = name
        self._value = 0
        self._peak = 0

    def set(self, value: int) -> None:
        """Set the gauge to ``value``, updating the peak."""

        self._value = value
        if value > self._peak:
            self._peak = value

    def add(self, delta: int) -> int:
        """Adjust the gauge by ``delta`` and return the new value."""

        self.set(self._value + delta)
        return self._value

    @property
    def value(self) -> int:
        """The current level."""

        return self._value

    @property
    def peak(self) -> int:
        """The highest level the gauge has ever held."""

        return self._peak

    def __repr__(self) -> str:
        return "Gauge({!r}={}, peak={})".format(
            self.name, self._value, self._peak
        )


class Histogram:
    """A distribution of observed numeric values.

    Records every observation and summarises them on demand via
    :mod:`taskflow.stats` (count, mean, min/max, percentiles). Suitable for job
    durations, attempt counts, or any series where the shape -- not just the sum
    -- matters.
    """

    __slots__ = ("name", "_values")

    def __init__(self, name: str) -> None:
        self.name = name
        self._values: List[float] = []

    def observe(self, value: float) -> None:
        """Record a single observation."""

        self._values.append(value)

    def values(self) -> List[float]:
        """Return the observed values, in observation order."""

        return list(self._values)

    def count(self) -> int:
        """Return the number of observations."""

        return len(self._values)

    def mean(self) -> float:
        """Return the mean observation (``0.0`` if none)."""

        return stats.mean(self._values)

    def percentile(self, pct: float) -> float:
        """Return the ``pct`` percentile of the observations."""

        return stats.percentile(self._values, pct)

    def summary(self) -> Dict[str, float]:
        """Return a compact statistical summary of the distribution."""

        return {
            "count": self.count(),
            "mean": self.mean(),
            "min": stats.minimum(self._values),
            "max": stats.maximum(self._values),
            "p50": self.percentile(50),
            "p95": self.percentile(95),
        }

    def __repr__(self) -> str:
        return "Histogram({!r}, count={})".format(self.name, self.count())


class Timer:
    """Measure virtual-time spans and feed their durations to a histogram.

    A timer is started for a key (a job id, say) at some virtual time and stopped
    later; the elapsed span is recorded in an internal :class:`Histogram`. Unlike
    a wall-clock timer it reads virtual time passed in explicitly, keeping the
    whole thing deterministic.
    """

    __slots__ = ("name", "_starts", "_hist")

    def __init__(self, name: str) -> None:
        self.name = name
        self._starts: Dict[str, int] = {}
        self._hist = Histogram(name)

    def start(self, key: str, now: int) -> None:
        """Mark ``key`` as started at virtual time ``now``."""

        self._starts[key] = now

    def stop(self, key: str, now: int) -> Optional[int]:
        """Record the span for ``key`` ending at ``now``; return the span.

        Returns ``None`` if the key was never started (a stop without a matching
        start is ignored rather than raising, so a partial event stream does not
        crash the collector).
        """

        if key not in self._starts:
            return None
        span = now - self._starts.pop(key)
        self._hist.observe(span)
        return span

    def histogram(self) -> Histogram:
        """Return the underlying duration histogram."""

        return self._hist

    def summary(self) -> Dict[str, float]:
        """Return the duration histogram's statistical summary."""

        return self._hist.summary()

    def __repr__(self) -> str:
        return "Timer({!r}, spans={})".format(self.name, self._hist.count())


class MetricRegistry:
    """A named namespace of metric primitives.

    Hands out counters, gauges, histograms and timers by name, creating each on
    first request and returning the same instance thereafter. Sharing a registry
    lets several observers contribute to one coherent set of metrics.
    """

    def __init__(self) -> None:
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._timers: Dict[str, Timer] = {}

    def counter(self, name: str) -> Counter:
        """Return (creating if needed) the counter called ``name``."""

        if name not in self._counters:
            self._counters[name] = Counter(name)
        return self._counters[name]

    def gauge(self, name: str) -> Gauge:
        """Return (creating if needed) the gauge called ``name``."""

        if name not in self._gauges:
            self._gauges[name] = Gauge(name)
        return self._gauges[name]

    def histogram(self, name: str) -> Histogram:
        """Return (creating if needed) the histogram called ``name``."""

        if name not in self._histograms:
            self._histograms[name] = Histogram(name)
        return self._histograms[name]

    def timer(self, name: str) -> Timer:
        """Return (creating if needed) the timer called ``name``."""

        if name not in self._timers:
            self._timers[name] = Timer(name)
        return self._timers[name]

    def snapshot(self) -> Dict[str, Any]:
        """Return a plain-dict view of every metric's current value/summary."""

        return {
            "counters": {n: c.value for n, c in self._counters.items()},
            "gauges": {
                n: {"value": g.value, "peak": g.peak}
                for n, g in self._gauges.items()
            },
            "histograms": {
                n: h.summary() for n, h in self._histograms.items()
            },
            "timers": {n: t.summary() for n, t in self._timers.items()},
        }

    def __repr__(self) -> str:
        return "MetricRegistry(counters={}, gauges={}, histograms={}, timers={})".format(
            len(self._counters),
            len(self._gauges),
            len(self._histograms),
            len(self._timers),
        )


class MetricsCollector:
    """Fold scheduler events into a :class:`MetricRegistry`.

    Construct with an :class:`~taskflow.events.EventBus` and the collector wires
    up its subscriptions immediately, so from that point on it maintains:

    * ``jobs.started`` / ``jobs.succeeded`` / ``jobs.failed`` / ``jobs.skipped``
      counters,
    * a ``jobs.running`` gauge (peak = observed peak concurrency),
    * a ``job.duration`` timer keyed by job id (start-to-terminal span), and
    * per-pool ``resource.<pool>`` gauges tracking held units and their peak.

    It reads only the event stream, exactly like :class:`taskflow.history.History`,
    so it never couples to scheduler internals.
    """

    def __init__(self, bus: Any, registry: Optional[MetricRegistry] = None) -> None:
        self.registry = registry if registry is not None else MetricRegistry()
        self._bus = bus
        self._subscribe()

    def _subscribe(self) -> None:
        self._bus.subscribe("job.started", self._on_started)
        self._bus.subscribe_prefix("job.term", self._on_terminal)
        self._bus.subscribe("resource.acquired", self._on_acquired)
        self._bus.subscribe("resource.released", self._on_released)

    def _on_started(self, event: Any) -> None:
        self.registry.counter("jobs.started").increment()
        gauge = self.registry.gauge("jobs.running")
        gauge.add(1)
        self.registry.timer("job.duration").start(event.get("id"), event.at)

    def _on_terminal(self, event: Any) -> None:
        suffix = event.topic.rsplit(".", 1)[-1]
        self.registry.counter("jobs.{}".format(suffix)).increment()
        # A skipped job never "started", so only decrement for real completions.
        if suffix in ("succeeded", "failed"):
            self.registry.gauge("jobs.running").add(-1)
            self.registry.timer("job.duration").stop(event.get("id"), event.at)

    def _on_acquired(self, event: Any) -> None:
        pool = event.get("pool")
        cost = event.get("cost", 0)
        self.registry.gauge("resource.{}".format(pool)).add(cost)

    def _on_released(self, event: Any) -> None:
        pool = event.get("pool")
        cost = event.get("cost", 0)
        self.registry.gauge("resource.{}".format(pool)).add(-cost)

    def snapshot(self) -> Dict[str, Any]:
        """Return the underlying registry's snapshot."""

        return self.registry.snapshot()

    def __repr__(self) -> str:
        return "MetricsCollector({!r})".format(self.registry)
