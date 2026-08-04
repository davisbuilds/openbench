"""The per-:class:`~taskflow.model.JobRun` lifecycle state machine.

Every job run is driven through its lifecycle by a :class:`StateMachine`. The
machine owns three responsibilities:

* **Legality.** Only the transitions in :data:`TRANSITIONS` are allowed; any
  other :meth:`StateMachine.advance` call raises :class:`IllegalTransition`.
* **Guards.** Some transitions carry an extra precondition beyond mere
  legality. In particular, moving a run from ``READY`` to ``RUNNING`` as part of
  a scheduler admission *batch* must respect the ready-queue's ordering
  invariant: within a single admission batch, runs are dispatched in
  non-increasing priority order.
* **Hooks.** Callers may register callbacks fired whenever the machine enters a
  given state, which the scheduler and tests use to observe progress.

The machine mutates the :class:`~taskflow.model.JobRun` it wraps (recording the
new state and a timeline entry) but performs no I/O and publishes no events --
narration is the scheduler's job.
"""

from __future__ import annotations

from taskflow.model import JobRun, State, TaskflowError

# The legal transition map: current state -> set of states reachable from it.
TRANSITIONS = {
    State.PENDING: {State.READY, State.SKIPPED, State.CANCELLED},
    State.READY: {State.RUNNING, State.SKIPPED, State.CANCELLED},
    State.RUNNING: {
        State.SUCCEEDED,
        State.FAILED,
        State.RETRYING,
        State.CANCELLED,
    },
    State.RETRYING: {State.READY, State.RUNNING, State.SKIPPED, State.CANCELLED},
    State.SUCCEEDED: set(),
    State.FAILED: set(),
    State.SKIPPED: set(),
    State.CANCELLED: set(),
}

# Sentinel distinguishing "no batch context supplied" from "priority is None".
_UNSET = object()


class IllegalTransition(TaskflowError):
    """Raised when a state transition is not permitted.

    Carries the ``from_state`` and ``to_state`` involved so callers (and error
    messages) can report exactly what was attempted.
    """

    def __init__(self, from_state, to_state, reason=None):
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        detail = "" if not reason else " ({})".format(reason)
        super().__init__(
            "illegal transition {} -> {}{}".format(
                from_state, to_state, detail
            )
        )


def batch_order_ok(prev_priority, current_priority):
    """Return ``True`` if dispatching ``current_priority`` after ``prev_priority``
    respects the admission-batch ordering invariant.

    The ready queue hands the scheduler tasks in non-increasing priority order
    within a single tick, so each task admitted in a batch must have priority
    less than or equal to the task admitted just before it. This guard asserts
    that invariant: a batch that tried to start a *higher* priority task after a
    lower one would mean the queue mis-ordered its output.
    """

    return current_priority >= prev_priority


class StateMachine:
    """Drives one :class:`~taskflow.model.JobRun` through its lifecycle.

    Parameters
    ----------
    run:
        The :class:`~taskflow.model.JobRun` to manage. Its ``state`` is the
        machine's authoritative current state.
    """

    def __init__(self, run):
        if not isinstance(run, JobRun):
            raise TypeError("StateMachine wraps a JobRun")
        self.run = run
        self._hooks = {}

    @property
    def state(self):
        """The current state of the wrapped run."""

        return self.run.state

    def register_hook(self, state, callback):
        """Fire ``callback(run, now)`` whenever the machine enters ``state``.

        Multiple hooks per state are supported and fire in registration order.
        """

        self._hooks.setdefault(state, []).append(callback)

    def can_advance(self, target):
        """Return ``True`` if ``target`` is a legal successor of the current state.

        This checks legality only; batch-ordering guards are evaluated by
        :meth:`advance` when the relevant context is supplied.
        """

        return target in TRANSITIONS[self.run.state]

    def valid_targets(self):
        """Return the set of states reachable from the current state."""

        return set(TRANSITIONS[self.run.state])

    def advance(self, target, now=0, batch_prev_priority=_UNSET):
        """Move the run to ``target``, validating legality and guards.

        Parameters
        ----------
        target:
            The state to move to.
        now:
            The virtual time stamped on the resulting timeline entry.
        batch_prev_priority:
            When admitting a run to ``RUNNING`` as part of a scheduler batch,
            the priority of the run admitted immediately before it in the same
            batch (or ``None`` for the first run in the batch). Supplying it
            enables the non-increasing-priority guard. Omit it entirely for
            transitions that are not batch admissions.

        Raises :class:`IllegalTransition` if the transition is not permitted or
        a guard rejects it.
        """

        current = self.run.state
        if target not in TRANSITIONS[current]:
            raise IllegalTransition(current, target)

        if target is State.RUNNING and batch_prev_priority is not _UNSET:
            # Admission-batch ordering guard: the run being started must not
            # have a higher priority than the run started just before it.
            if batch_prev_priority is not None and not batch_order_ok(
                batch_prev_priority, self.run.priority
            ):
                raise IllegalTransition(
                    current,
                    target,
                    reason="priority {} dispatched after {} in one batch".format(
                        self.run.priority, batch_prev_priority
                    ),
                )

        self.run.record_state(target, now)
        for callback in self._hooks.get(target, []):
            callback(self.run, now)
        return target

    def force(self, target, now=0):
        """Transition without the batch guard, still enforcing legality.

        Used by the scheduler for transitions it drives directly (skips,
        cancellations, retries) where no admission batch is involved.
        """

        return self.advance(target, now=now)

    def __repr__(self):
        return "StateMachine(run={!r}, state={})".format(
            self.run.id, self.run.state
        )
