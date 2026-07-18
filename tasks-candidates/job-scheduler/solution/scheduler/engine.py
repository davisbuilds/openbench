"""The deterministic run loop.

A single-threaded, stepwise simulation — no real threads or async. Each step
selects the jobs whose dependencies have all SUCCEEDED, orders them by priority
(then id), admits as many as the concurrency limit and the resource pool allow,
runs them, and retries or fails each per its retry budget. When a job fails
permanently, every job downstream of it is marked SKIPPED and never run.

The loop is fully determined by the jobs, the concurrency limit, and the pool,
so a given scenario always produces the same history.
"""

from .graph import CycleError, Graph
from .history import Event, History
from .jobs import Status


class Engine:
    def __init__(self, jobs, concurrency=1, pool=None):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        # Preserve construction order; it is the final tie-break for determinism.
        self.jobs = {}
        for job in jobs:
            if job.id in self.jobs:
                raise ValueError("duplicate job id: {}".format(job.id))
            self.jobs[job.id] = job
        self.concurrency = concurrency
        self.pool = pool
        self.history = History()

        self.graph = Graph()
        for job in jobs:
            self.graph.add_node(job.id)
        for job in jobs:
            for dep in job.deps:
                self.graph.add_edge(dep, job.id)

    # -- status queries -----------------------------------------------------

    def jobs_by_status(self, status):
        """Ids of jobs currently in ``status``, in construction order."""
        return [jid for jid, job in self.jobs.items() if job.status is status]

    def succeeded(self):
        return self.jobs_by_status(Status.SUCCEEDED)

    def failed(self):
        return self.jobs_by_status(Status.FAILED)

    def skipped(self):
        return self.jobs_by_status(Status.SKIPPED)

    def pending(self):
        return self.jobs_by_status(Status.PENDING)

    def is_done(self):
        """True once no job is still PENDING or RUNNING."""
        return all(job.is_terminal() for job in self.jobs.values())

    # -- scheduling ---------------------------------------------------------

    def _ready_jobs(self):
        """PENDING jobs whose every dependency has SUCCEEDED."""
        ready = []
        for job in self.jobs.values():
            if job.status is not Status.PENDING:
                continue
            if all(self.jobs[d].status is Status.SUCCEEDED for d in job.deps):
                ready.append(job)
        return ready

    def _admit(self, ready):
        """Pick the jobs to run this step, honouring concurrency and capacity.

        ``ready`` is already ordered best-first. A job that does not fit the
        pool right now is skipped over (a lower-cost, lower-priority job behind
        it may still fit), but the concurrency limit caps the batch size.
        """
        admitted = []
        for job in ready:
            if len(admitted) >= self.concurrency:
                break
            if self.pool is not None and not self.pool.can_admit(job.cost):
                continue
            if self.pool is not None:
                self.pool.acquire(job.cost)
            admitted.append(job)
        return admitted

    # -- execution ----------------------------------------------------------

    def _run_job(self, job):
        """Run one job to a terminal state, retrying up to its budget.

        The first run is attempt 1. After a failed attempt the job retries while
        it still has budget left (``attempts <= max_retries``); the attempt that
        exhausts the budget fails permanently.
        """
        while True:
            job.attempts += 1
            job.status = Status.RUNNING
            self.history.record(job.id, Event.STARTED)
            try:
                if job.run is not None:
                    job.run()
            except Exception:
                if job.attempts <= job.max_retries:
                    self.history.record(job.id, Event.RETRIED)
                    continue
                job.status = Status.FAILED
                self.history.record(job.id, Event.FAILED)
                return
            job.status = Status.SUCCEEDED
            self.history.record(job.id, Event.SUCCEEDED)
            return

    def _skip_dependents(self, failed_job):
        """Mark everything downstream of a permanent failure as SKIPPED."""
        for dep_id in self.graph.transitive_dependents(failed_job.id):
            dependent = self.jobs[dep_id]
            if dependent.status is Status.PENDING:
                dependent.status = Status.SKIPPED
                self.history.record(dep_id, Event.SKIPPED)

    # -- driver -------------------------------------------------------------

    def run(self):
        """Run every job that can run and return the event history.

        Refuses to start on a cyclic dependency graph — such a run could never
        drain — surfacing it as a :class:`CycleError` rather than looping.
        """
        if self.graph.has_cycle():
            raise CycleError("job graph contains a cycle")
        while True:
            ready = self._ready_jobs()
            if not ready:
                break
            ready.sort(key=lambda j: (-j.priority, j.id))
            admitted = self._admit(ready)
            if not admitted:
                # Nothing fits (a job costs more than the pool can ever give);
                # stop rather than spin forever.
                break
            for job in admitted:
                self._run_job(job)
                if self.pool is not None:
                    self.pool.release(job.cost)
                if job.status is Status.FAILED:
                    self._skip_dependents(job)
        return self.history
