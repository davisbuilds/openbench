"""Job definitions and the status lifecycle used by the engine.

A :class:`Job` is a passive record: an id, the ids it depends on, a scheduling
priority, a retry budget, a resource cost, and a ``run`` callable that performs
the work (and may raise to signal failure). The engine owns all state
transitions; a freshly constructed job is always ``PENDING`` with zero attempts.
"""

import enum


class Status(enum.Enum):
    """Lifecycle of a single job.

    A job starts ``PENDING``. The engine flips it to ``RUNNING`` while its
    ``run`` callable executes, then to ``SUCCEEDED`` or ``FAILED``. A job whose
    upstream dependency failed permanently is never run and ends ``SKIPPED``.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class Job:
    """A unit of work with dependencies and a retry budget.

    Parameters
    ----------
    id:
        Unique identifier within a run.
    run:
        Zero-argument callable performing the work. Raising any exception marks
        the attempt as failed. ``None`` is treated as an always-succeeding no-op.
    deps:
        Ids of jobs that must SUCCEED before this job becomes ready.
    priority:
        Higher runs first among jobs that are ready at the same time. Ties on
        priority are broken by id (ascending) for determinism.
    max_retries:
        Number of *additional* attempts allowed after the first. A job with
        ``max_retries=2`` may run up to three times before failing permanently.
    cost:
        Integer amount of the shared resource pool this job occupies while it
        runs. Released when the job finishes, whether it succeeds or fails.
    """

    def __init__(self, id, run=None, deps=(), priority=0, max_retries=0, cost=1):
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if cost < 0:
            raise ValueError("cost must be non-negative")
        self.id = id
        self.run = run
        self.deps = list(deps)
        self.priority = priority
        self.max_retries = max_retries
        self.cost = cost
        self.status = Status.PENDING
        self.attempts = 0

    def is_terminal(self):
        """True once the job has reached a final state."""
        return self.status in (Status.SUCCEEDED, Status.FAILED, Status.SKIPPED)

    def has_run(self):
        """True once at least one attempt has been made."""
        return self.attempts > 0

    @property
    def remaining_retries(self):
        """Retry attempts still available given the attempts made so far."""
        used = max(self.attempts - 1, 0)
        return max(self.max_retries - used, 0)

    def reset(self):
        """Return the job to its initial PENDING state (for reuse in tests)."""
        self.status = Status.PENDING
        self.attempts = 0

    def __repr__(self):
        return "Job({!r}, status={}, attempts={})".format(
            self.id, self.status.name, self.attempts)
