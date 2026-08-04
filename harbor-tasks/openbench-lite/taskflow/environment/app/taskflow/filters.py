"""Composable predicates for selecting tasks and runs.

:class:`taskflow.model.Pipeline` exposes a ``filter(predicate)`` method and many
tools want to slice a pipeline or a set of runs by some criterion -- "the tasks
that use the ``db`` pool", "the runs that were retried", "the high-priority
leaves". Writing those predicates inline is fine once, but they read much better
when built from small, named, combinable pieces.

This module provides a :class:`Predicate` wrapper that supports the boolean
operators (``&``, ``|``, ``~``) plus a library of factory functions returning
ready-made predicates over :class:`~taskflow.model.Task` and
:class:`~taskflow.model.JobRun` objects. A ``Predicate`` is itself callable, so
it drops straight into ``pipeline.filter(...)`` or a plain ``filter()`` /
comprehension.

The predicates never mutate what they inspect and hold no state, so they are
safe to build once and reuse across pipelines.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, List

from taskflow.model import State

PredicateFn = Callable[[Any], bool]


class Predicate:
    """A callable boolean test that composes with ``&``, ``|`` and ``~``.

    Wrap any ``obj -> bool`` function and gain algebraic composition::

        selectable = has_dependencies & ~uses_resources
        chosen = pipeline.filter(selectable)

    The wrapped function is called with a single argument (a ``Task`` or
    ``JobRun`` depending on the predicate) and must return something truthy or
    falsy. Composition is short-circuiting in the usual Python way.
    """

    __slots__ = ("_fn", "_name")

    def __init__(self, fn: PredicateFn, name: str = "predicate") -> None:
        self._fn = fn
        self._name = name

    def __call__(self, obj: Any) -> bool:
        return bool(self._fn(obj))

    def __and__(self, other: "Predicate") -> "Predicate":
        return Predicate(
            lambda obj: self(obj) and other(obj),
            "({} & {})".format(self._name, other._name),
        )

    def __or__(self, other: "Predicate") -> "Predicate":
        return Predicate(
            lambda obj: self(obj) or other(obj),
            "({} | {})".format(self._name, other._name),
        )

    def __invert__(self) -> "Predicate":
        return Predicate(
            lambda obj: not self(obj), "~{}".format(self._name)
        )

    @property
    def name(self) -> str:
        """A human-readable rendering of how the predicate was built."""

        return self._name

    def filter(self, items: Iterable[Any]) -> List[Any]:
        """Return the items for which this predicate holds, order preserved."""

        return [item for item in items if self(item)]

    def any(self, items: Iterable[Any]) -> bool:
        """Return ``True`` if the predicate holds for at least one item."""

        return any(self(item) for item in items)

    def all(self, items: Iterable[Any]) -> bool:
        """Return ``True`` if the predicate holds for every item."""

        return all(self(item) for item in items)

    def count(self, items: Iterable[Any]) -> int:
        """Return how many items satisfy the predicate."""

        return sum(1 for item in items if self(item))

    def __repr__(self) -> str:
        return "Predicate({})".format(self._name)


def always() -> Predicate:
    """A predicate that is true for everything."""

    return Predicate(lambda _obj: True, "always")


def never() -> Predicate:
    """A predicate that is false for everything."""

    return Predicate(lambda _obj: False, "never")


def all_of(*predicates: Predicate) -> Predicate:
    """Conjoin several predicates: true only when *every* one holds.

    With no arguments this is vacuously true (the neutral element of ``and``),
    which lets callers fold a possibly-empty list of filters without a special
    case.
    """

    parts = list(predicates)
    return Predicate(
        lambda obj: all(p(obj) for p in parts),
        "all_of({})".format(", ".join(p.name for p in parts)) if parts else "always",
    )


def any_of(*predicates: Predicate) -> Predicate:
    """Disjoin several predicates: true when *any* one holds.

    With no arguments this is vacuously false (the neutral element of ``or``).
    """

    parts = list(predicates)
    return Predicate(
        lambda obj: any(p(obj) for p in parts),
        "any_of({})".format(", ".join(p.name for p in parts)) if parts else "never",
    )


# -- Task predicates ------------------------------------------------------


def has_dependencies() -> Predicate:
    """Match tasks that declare at least one dependency."""

    return Predicate(lambda task: bool(task.deps), "has_dependencies")


def is_root() -> Predicate:
    """Match tasks with no dependencies (pipeline entry points)."""

    return Predicate(lambda task: not task.deps, "is_root")


def uses_resources() -> Predicate:
    """Match tasks that charge a positive cost to any resource pool."""

    return Predicate(lambda task: task.uses_resources(), "uses_resources")


def uses_pool(pool_name: str) -> Predicate:
    """Match tasks that charge a positive cost to ``pool_name`` specifically."""

    return Predicate(
        lambda task: task.requires(pool_name) > 0,
        "uses_pool({!r})".format(pool_name),
    )


def priority_at_least(threshold: int) -> Predicate:
    """Match tasks whose priority is ``>= threshold``."""

    return Predicate(
        lambda task: task.priority >= threshold,
        "priority_at_least({})".format(threshold),
    )


def priority_equals(value: int) -> Predicate:
    """Match tasks whose priority is exactly ``value``."""

    return Predicate(
        lambda task: task.priority == value,
        "priority_equals({})".format(value),
    )


def retryable() -> Predicate:
    """Match tasks whose retry policy permits more than a single attempt."""

    return Predicate(
        lambda task: task.retry_policy.max_attempts > 1, "retryable"
    )


def depends_on(dep_id: str) -> Predicate:
    """Match tasks that directly declare ``dep_id`` as a dependency."""

    return Predicate(
        lambda task: dep_id in task.deps, "depends_on({!r})".format(dep_id)
    )


def id_in(ids: Iterable[str]) -> Predicate:
    """Match tasks (or runs) whose id is in ``ids``."""

    wanted = set(ids)
    return Predicate(lambda obj: obj.id in wanted, "id_in(...)")


def metadata_equals(key: str, value: Any) -> Predicate:
    """Match tasks whose ``metadata[key]`` equals ``value``.

    A task missing the key does not match. Handy for slicing a pipeline by a
    caller-supplied tag such as ``metadata_equals("team", "billing")``.
    """

    return Predicate(
        lambda task: task.metadata.get(key) == value,
        "metadata_equals({!r}, {!r})".format(key, value),
    )


def has_metadata(key: str) -> Predicate:
    """Match tasks that carry ``key`` in their metadata, whatever its value."""

    return Predicate(
        lambda task: key in task.metadata, "has_metadata({!r})".format(key)
    )


def duration_at_least(ticks: int) -> Predicate:
    """Match tasks whose single-attempt duration is ``>= ticks``."""

    return Predicate(
        lambda task: task.duration >= ticks,
        "duration_at_least({})".format(ticks),
    )


# -- JobRun predicates ----------------------------------------------------


def in_state(state: State) -> Predicate:
    """Match runs currently (or finally) in ``state``."""

    return Predicate(
        lambda run: run.state is state, "in_state({})".format(state)
    )


def succeeded() -> Predicate:
    """Match runs that finished in ``SUCCEEDED``."""

    return Predicate(lambda run: run.state is State.SUCCEEDED, "succeeded")


def failed() -> Predicate:
    """Match runs that finished in ``FAILED``."""

    return Predicate(lambda run: run.state is State.FAILED, "failed")


def skipped() -> Predicate:
    """Match runs that were ``SKIPPED``."""

    return Predicate(lambda run: run.state is State.SKIPPED, "skipped")


def terminal() -> Predicate:
    """Match runs that have reached any terminal state."""

    return Predicate(lambda run: run.state.is_terminal(), "terminal")


def was_retried() -> Predicate:
    """Match runs that passed through ``RETRYING`` at least once."""

    return Predicate(lambda run: run.was_retried(), "was_retried")


def attempted_more_than(n: int) -> Predicate:
    """Match runs that made more than ``n`` attempts."""

    return Predicate(
        lambda run: run.attempts > n, "attempted_more_than({})".format(n)
    )
