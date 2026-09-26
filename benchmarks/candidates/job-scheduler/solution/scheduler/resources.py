"""A fixed-capacity resource pool jobs draw on while running.

Each job declares an integer ``cost``. The engine acquires that cost before
running a job and releases it afterwards, on both the success and the failure
path, so capacity is never leaked. The pool must never admit beyond capacity.
"""


class ResourceError(Exception):
    """Raised on an attempt to acquire more than the pool can provide."""


class ResourcePool:
    def __init__(self, capacity):
        if capacity < 0:
            raise ValueError("capacity must be non-negative")
        self.capacity = capacity
        self.in_use = 0

    def available(self):
        """Units currently free."""
        return self.capacity - self.in_use

    def is_full(self):
        """True when no capacity is free."""
        return self.available() == 0

    def utilization(self):
        """Fraction of capacity in use, in ``[0.0, 1.0]``."""
        if self.capacity == 0:
            return 0.0
        return self.in_use / self.capacity

    def can_admit(self, cost):
        """True if ``cost`` units could be acquired right now."""
        return cost <= self.available()

    def acquire(self, cost):
        """Reserve ``cost`` units, or raise if that would exceed capacity."""
        if cost < 0:
            raise ValueError("cost must be non-negative")
        if not self.can_admit(cost):
            raise ResourceError(
                "cannot admit cost {} (available {})".format(cost, self.available()))
        self.in_use += cost

    def release(self, cost):
        """Return ``cost`` units to the pool.

        Releasing the same amount that was acquired restores capacity exactly;
        this is called for every job that finishes, whether it succeeded or
        failed.
        """
        self.in_use -= cost
        if self.in_use < 0:
            self.in_use = 0

    def __repr__(self):
        return "ResourcePool(in_use={}, capacity={})".format(
            self.in_use, self.capacity)
