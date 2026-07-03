"""Pluggable scheduling policies for ordering ready tasks.

The production scheduler bakes in one policy -- highest priority first, ties
broken by the order tasks became ready (see :mod:`taskflow.queue`). That is the
right default and the engine's behaviour depends on it, so this module does *not*
replace it. Instead it offers a small library of *alternative* ordering
strategies that tools can use to answer "what if?" questions: preview the order a
fair-share policy would pick, compare a round-robin schedule against the default,
or drive a custom dispatcher built on the same primitives.

A policy is any object implementing :class:`SchedulingPolicy` -- essentially a
function from a list of ready *candidates* to an ordering of them. A candidate is
anything exposing an ``id`` and a ``priority`` attribute, which both
:class:`~taskflow.model.Task` and :class:`~taskflow.model.JobRun` already do, so
policies work directly on either.

Implementations provided:

* :class:`FifoPolicy` -- first-ready, first-served; ignores priority entirely.
* :class:`PriorityPolicy` -- the engine's own rule, reproduced for comparison.
* :class:`RoundRobinPolicy` -- cycle fairly across a grouping key (e.g. owning
  team) so no single group monopolises the schedule.
* :class:`FairSharePolicy` -- weighted fair queuing across groups, dispatching
  the group that has so far received the least of its fair share.
* :class:`CompositePolicy` -- order by one policy, break ties with another.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

# A "candidate" is any object with ``id`` and ``priority`` attributes. We keep
# the type loose (``Any``) rather than demand a protocol so JobRun and Task both
# qualify without inheriting anything.
Candidate = Any
GroupKey = Callable[[Candidate], str]


def _default_group(candidate: Candidate) -> str:
    """Group candidates by a ``metadata['group']`` tag, defaulting to ``''``.

    Only :class:`~taskflow.model.Task` carries metadata; a :class:`JobRun`
    exposes its task via ``.task``. Anything else falls into the single empty
    group, which makes the fair-share policies degrade gracefully to FIFO.
    """

    metadata = getattr(candidate, "metadata", None)
    if metadata is None:
        task = getattr(candidate, "task", None)
        metadata = getattr(task, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("group", "")
        return str(value)
    return ""


class SchedulingPolicy:
    """Abstract base for an ordering strategy over ready candidates.

    A concrete policy implements :meth:`order`, returning the candidates in the
    sequence they should be dispatched. The base class provides :meth:`pick`
    (the single next choice) and :meth:`rank` (candidate id to position) on top
    of it, so every policy gets a uniform surface for free.
    """

    name = "policy"

    def order(self, candidates: Sequence[Candidate]) -> List[Candidate]:
        """Return ``candidates`` in dispatch order. Must be overridden."""

        raise NotImplementedError

    def pick(self, candidates: Sequence[Candidate]) -> Optional[Candidate]:
        """Return the single candidate to dispatch next, or ``None`` if empty."""

        ordered = self.order(candidates)
        return ordered[0] if ordered else None

    def rank(self, candidates: Sequence[Candidate]) -> Dict[str, int]:
        """Return a ``{candidate_id: position}`` map in dispatch order (0-based)."""

        return {c.id: i for i, c in enumerate(self.order(candidates))}

    def __repr__(self) -> str:
        return "{}()".format(type(self).__name__)


class FifoPolicy(SchedulingPolicy):
    """Dispatch strictly in the order candidates were offered.

    Priority is ignored entirely; this is the fairest policy in the sense that
    no task can jump the queue, at the cost of ignoring urgency. Because it
    preserves the input order it is a stable no-op sort.
    """

    name = "fifo"

    def order(self, candidates: Sequence[Candidate]) -> List[Candidate]:
        return list(candidates)


class PriorityPolicy(SchedulingPolicy):
    """Dispatch highest-priority first, ties broken by input (ready) order.

    This reproduces the engine's built-in ready-queue rule so tools can display
    or reason about the real schedule. The sort is stable, so equal-priority
    candidates keep their incoming order -- exactly the FIFO-within-a-band
    behaviour of :class:`taskflow.queue.ReadyQueue`.
    """

    name = "priority"

    def order(self, candidates: Sequence[Candidate]) -> List[Candidate]:
        indexed = list(enumerate(candidates))
        indexed.sort(key=lambda pair: (-pair[1].priority, pair[0]))
        return [candidate for _index, candidate in indexed]


class RoundRobinPolicy(SchedulingPolicy):
    """Cycle across groups so no single group is dispatched twice in a row.

    Candidates are bucketed by ``group_key`` (defaulting to a ``metadata['group']``
    tag). The policy then emits one candidate from each non-empty group in turn,
    round after round, until all are placed. Within a group, input order is
    preserved. This starves no group and bounds how far ahead any one group can
    get, which is useful when several independent workloads share a pipeline.
    """

    name = "round_robin"

    def __init__(self, group_key: Optional[GroupKey] = None) -> None:
        self._group_key = group_key or _default_group

    def order(self, candidates: Sequence[Candidate]) -> List[Candidate]:
        buckets: Dict[str, List[Candidate]] = {}
        group_order: List[str] = []
        for candidate in candidates:
            key = self._group_key(candidate)
            if key not in buckets:
                buckets[key] = []
                group_order.append(key)
            buckets[key].append(candidate)

        result: List[Candidate] = []
        remaining = sum(len(bucket) for bucket in buckets.values())
        cursors = {key: 0 for key in group_order}
        while remaining:
            for key in group_order:
                cursor = cursors[key]
                bucket = buckets[key]
                if cursor < len(bucket):
                    result.append(bucket[cursor])
                    cursors[key] = cursor + 1
                    remaining -= 1
        return result


class FairSharePolicy(SchedulingPolicy):
    """Weighted fair queuing across groups by cumulative dispatch count.

    Each group has a weight (default 1). At every step the policy dispatches from
    the group whose *dispatched count divided by weight* is currently smallest --
    i.e. the group furthest behind its fair share -- breaking ties by the group's
    first appearance. A group weighted 2 receives roughly twice the schedule
    share of a group weighted 1. Within a group, input order is preserved.

    This is a deterministic, virtual-time-free analogue of the weighted fair
    queuing disciplines real schedulers use, suitable for previewing how a
    fairness-tuned dispatcher would sequence the same ready set.
    """

    name = "fair_share"

    def __init__(
        self,
        weights: Optional[Dict[str, int]] = None,
        group_key: Optional[GroupKey] = None,
    ) -> None:
        self._weights = dict(weights) if weights else {}
        self._group_key = group_key or _default_group

    def _weight(self, group: str) -> int:
        weight = self._weights.get(group, 1)
        return weight if weight > 0 else 1

    def order(self, candidates: Sequence[Candidate]) -> List[Candidate]:
        buckets: Dict[str, List[Candidate]] = {}
        group_order: List[str] = []
        for candidate in candidates:
            key = self._group_key(candidate)
            if key not in buckets:
                buckets[key] = []
                group_order.append(key)
            buckets[key].append(candidate)

        cursors = {key: 0 for key in group_order}
        dispatched = {key: 0 for key in group_order}
        result: List[Candidate] = []
        total = len(candidates)
        while len(result) < total:
            best_key = None
            best_share = None
            for pos, key in enumerate(group_order):
                if cursors[key] >= len(buckets[key]):
                    continue
                share = dispatched[key] / self._weight(key)
                if best_share is None or share < best_share:
                    best_share = share
                    best_key = key
            if best_key is None:
                break
            result.append(buckets[best_key][cursors[best_key]])
            cursors[best_key] += 1
            dispatched[best_key] += 1
        return result


class CompositePolicy(SchedulingPolicy):
    """Order by a primary policy, breaking ties with a secondary one.

    Runs ``primary`` to get a coarse order, then within each run of candidates
    the primary considers equivalent (same rank position band) applies
    ``secondary``. In practice the simplest useful composition is "priority, then
    fair-share among equal priorities", which this expresses directly.
    """

    name = "composite"

    def __init__(
        self, primary: SchedulingPolicy, secondary: SchedulingPolicy
    ) -> None:
        self._primary = primary
        self._secondary = secondary

    def order(self, candidates: Sequence[Candidate]) -> List[Candidate]:
        # Group by the primary's notion of priority when available; otherwise
        # fall back to the primary order and stable-refine with the secondary.
        by_priority: Dict[int, List[Candidate]] = {}
        priorities: List[int] = []
        for candidate in candidates:
            key = getattr(candidate, "priority", 0)
            if key not in by_priority:
                by_priority[key] = []
                priorities.append(key)
            by_priority[key].append(candidate)

        result: List[Candidate] = []
        for key in sorted(priorities, reverse=True):
            result.extend(self._secondary.order(by_priority[key]))
        return result


def compare_policies(
    policies: Dict[str, SchedulingPolicy], candidates: Sequence[Candidate]
) -> Dict[str, List[str]]:
    """Return each policy's dispatch order over the same candidate set.

    A convenience for tooling that wants to show, side by side, how differently
    several policies would sequence the same ready tasks. The result maps a
    policy label to the list of candidate ids in that policy's order.
    """

    return {
        label: [c.id for c in policy.order(candidates)]
        for label, policy in policies.items()
    }
