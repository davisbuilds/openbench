"""Named resource pools with integer capacities and admission control.

Many pipelines need to bound how much of some scarce resource runs at once: a
database connection pool, a memory budget, a licence count. A
:class:`ResourcePool` models one such resource as an integer capacity; a task
declares how many units it holds while running via its ``resource_costs``
mapping, and the :class:`ResourceManager` grants or refuses admission across
all pools atomically.

The invariants the manager guarantees:

* A task is admitted only if *every* pool it touches has enough free capacity
  for its full cost -- admission is all-or-nothing across pools.
* Usage never exceeds capacity. The manager never over-admits.
* Whatever a task acquired on admission is released in full when it finishes,
  whether it succeeded or failed. Costs are symmetric: you release exactly what
  you acquired.
"""

from __future__ import annotations

from taskflow.model import ConfigError, TaskflowError


class ResourceError(TaskflowError):
    """Raised on an invalid pool operation (e.g. releasing more than held)."""


class ResourcePool:
    """A single named resource with a fixed integer capacity.

    The pool tracks ``used`` units in ``[0, capacity]``. :meth:`can_admit`
    tests whether a cost would fit; :meth:`acquire` reserves it (raising if it
    would overflow); :meth:`release` returns units. The pool also records the
    peak usage ever reached so reporting can show how close a run came to
    saturating it.
    """

    __slots__ = ("name", "capacity", "used", "peak")

    def __init__(self, name, capacity):
        if not isinstance(capacity, int) or capacity < 0:
            raise ConfigError(
                "resource pool {!r} capacity must be a non-negative "
                "integer".format(name)
            )
        self.name = name
        self.capacity = capacity
        self.used = 0
        self.peak = 0

    def available(self):
        """Return the number of free units right now."""

        return self.capacity - self.used

    def can_admit(self, cost):
        """Return ``True`` if ``cost`` units would fit without overflowing."""

        if cost < 0:
            raise ResourceError("cost must be non-negative")
        return self.used + cost <= self.capacity

    def acquire(self, cost):
        """Reserve ``cost`` units, updating the peak. Raises if it won't fit.

        Callers should gate this with :meth:`can_admit`; the guard here is a
        safety net that enforces the never-over-admit invariant.
        """

        if cost < 0:
            raise ResourceError("cost must be non-negative")
        if self.used + cost > self.capacity:
            raise ResourceError(
                "pool {!r} would over-admit: used={} cost={} capacity={}".format(
                    self.name, self.used, cost, self.capacity
                )
            )
        self.used += cost
        if self.used > self.peak:
            self.peak = self.used

    def release(self, cost):
        """Return ``cost`` units to the pool.

        The amount released must equal what was acquired for the finishing
        task; releasing more than is currently held is a bug in the caller and
        is rejected rather than silently clamped, because a negative usage
        would corrupt every later admission decision.
        """

        if cost < 0:
            raise ResourceError("cost must be non-negative")
        if cost > self.used:
            raise ResourceError(
                "pool {!r} would release more than held: used={} "
                "release={}".format(self.name, self.used, cost)
            )
        self.used -= cost

    def reset(self):
        """Return the pool to empty, keeping its recorded peak."""

        self.used = 0

    def __repr__(self):
        return "ResourcePool(name={!r}, used={}/{}, peak={})".format(
            self.name, self.used, self.capacity, self.peak
        )


class ResourceManager:
    """Admission control across a set of named pools.

    Construct from a ``{name: capacity}`` mapping. The manager exposes a single
    all-or-nothing :meth:`try_admit` that reserves a whole cost mapping if and
    only if every pool can satisfy its share, and a symmetric :meth:`release`
    that returns exactly those units.
    """

    def __init__(self, capacities=None):
        self._pools = {}
        for name, capacity in (capacities or {}).items():
            self._pools[name] = ResourcePool(name, capacity)

    def pool(self, name):
        """Return the named pool, creating an unbounded-by-error miss guard."""

        if name not in self._pools:
            raise ResourceError("unknown resource pool: {!r}".format(name))
        return self._pools[name]

    def has_pool(self, name):
        """Return ``True`` if a pool with ``name`` exists."""

        return name in self._pools

    def can_admit(self, costs):
        """Return ``True`` if every pool in ``costs`` can satisfy its cost.

        A cost mapping that references an unknown pool is rejected. An empty or
        ``None`` cost mapping always admits (the task uses no managed
        resource).
        """

        if not costs:
            return True
        for name, cost in costs.items():
            if cost <= 0:
                continue
            if name not in self._pools:
                raise ResourceError(
                    "unknown resource pool: {!r}".format(name)
                )
            if not self._pools[name].can_admit(cost):
                return False
        return True

    def try_admit(self, costs):
        """Atomically reserve ``costs`` across all pools.

        Returns ``True`` and reserves every unit if admission succeeds, or
        returns ``False`` and reserves nothing if any pool cannot fit its
        share. There is never a partial reservation.
        """

        if not self.can_admit(costs):
            return False
        if costs:
            for name, cost in costs.items():
                if cost > 0:
                    self._pools[name].acquire(cost)
        return True

    def release(self, costs):
        """Release ``costs`` back to their pools (the inverse of admission).

        The amounts must match what was acquired for the finishing task -- the
        engine always releases the task's own ``resource_costs`` -- so each
        pool gives back exactly what it lent.
        """

        if not costs:
            return
        for name, cost in costs.items():
            if cost > 0:
                self._pools[name].release(cost)

    def usage(self, name):
        """Return the current used units of a pool."""

        return self.pool(name).used

    def peak_usage(self, name):
        """Return the peak used units a pool ever reached."""

        return self.pool(name).peak

    def snapshot(self):
        """Return a ``{name: (used, capacity)}`` view of every pool."""

        return {
            name: (pool.used, pool.capacity)
            for name, pool in self._pools.items()
        }

    def pool_names(self):
        """Return the names of all managed pools in insertion order."""

        return list(self._pools.keys())

    def __repr__(self):
        return "ResourceManager({})".format(self.snapshot())
