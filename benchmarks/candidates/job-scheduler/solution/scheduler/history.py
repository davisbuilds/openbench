"""An ordered event log of everything the engine does.

Events are appended in the exact order they occur, so the log is the ground
truth for reconstructing what happened: the order jobs succeeded, how many times
each retried, and which jobs were skipped.
"""

import enum


class Event(enum.Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRIED = "retried"
    SKIPPED = "skipped"


class History:
    def __init__(self):
        self._log = []  # list of (job_id, Event) in occurrence order

    def record(self, job_id, event):
        self._log.append((job_id, event))

    def events(self):
        """The full log as a list of ``(job_id, Event)`` pairs."""
        return list(self._log)

    def __len__(self):
        return len(self._log)

    def events_for(self, job_id):
        """Every event logged for ``job_id``, in order."""
        return [event for jid, event in self._log if jid == job_id]

    def count(self, event):
        """How many times ``event`` was logged across all jobs."""
        return sum(1 for _jid, ev in self._log if ev is event)

    def run_order(self):
        """Ids in the order they SUCCEEDED.

        This preserves execution order — it is not sorted — so a higher-priority
        job that ran first appears first even if its id sorts later.
        """
        return [job_id for job_id, event in self._log if event is Event.SUCCEEDED]

    def retries_for(self, job_id):
        """How many times ``job_id`` was retried (RETRIED events logged)."""
        return sum(1 for jid, event in self._log
                   if jid == job_id and event is Event.RETRIED)

    def _first_seen(self, wanted):
        """Ids for which ``wanted`` was logged, in first-occurrence order."""
        out = []
        for job_id, event in self._log:
            if event is wanted and job_id not in out:
                out.append(job_id)
        return out

    def succeeded_jobs(self):
        """Ids that succeeded, in the order they first succeeded."""
        return self._first_seen(Event.SUCCEEDED)

    def failed_jobs(self):
        """Ids that failed permanently, in the order they failed."""
        return self._first_seen(Event.FAILED)

    def skipped_jobs(self):
        """Ids that were skipped, in the order they were skipped (deduped)."""
        return self._first_seen(Event.SKIPPED)
