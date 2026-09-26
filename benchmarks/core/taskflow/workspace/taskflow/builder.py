"""A fluent builder for constructing pipeline configuration dictionaries.

Writing a pipeline config by hand as a nested dict is fine for small examples,
but it gets error-prone as the graph grows: you repeat the ``{"id": ..., "deps":
[...]}`` boilerplate, mistype a dependency id, or forget the ``defaults`` block.
This module offers a small, chainable builder that produces exactly the same
dict shape :func:`taskflow.config.load_pipeline` consumes, while catching obvious
mistakes (an unknown dependency, a duplicate id) at build time.

The builder is deliberately a *config producer*, not a pipeline: it emits a plain
dict so the result stays serialisable, templatable and validatable. Call
:meth:`PipelineBuilder.build` for the dict, or the convenience
:meth:`PipelineBuilder.load` to go straight to a :class:`~taskflow.model.Pipeline`.

Example::

    config = (
        PipelineBuilder("etl")
        .defaults(retry={"max_attempts": 2})
        .task("extract")
        .task("transform", deps=["extract"], priority=5)
        .task("load", deps=["transform"], resources={"db": 1})
        .build()
    )
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from taskflow.model import ConfigError, Pipeline


class TaskBuilder:
    """A handle to one task within a :class:`PipelineBuilder`.

    Returned by :meth:`PipelineBuilder.task`, it lets you refine a single task
    with further chained calls (``.with_retry(...)``, ``.needs(...)``) while
    still forwarding unknown attribute access back to the parent builder, so a
    chain of ``.task(...).task(...)`` reads naturally.
    """

    __slots__ = ("_parent", "_spec")

    def __init__(self, parent: "PipelineBuilder", spec: Dict[str, Any]) -> None:
        self._parent = parent
        self._spec = spec

    def needs(self, *dep_ids: str) -> "TaskBuilder":
        """Add one or more dependencies to this task."""

        deps = self._spec.setdefault("deps", [])
        for dep in dep_ids:
            if dep not in deps:
                deps.append(dep)
        return self

    def with_priority(self, priority: int) -> "TaskBuilder":
        """Set this task's scheduling priority."""

        self._spec["priority"] = priority
        return self

    def with_retry(
        self,
        max_attempts: int,
        backoff: str = "constant",
        base: int = 0,
        factor: int = 2,
    ) -> "TaskBuilder":
        """Attach a retry policy block to this task."""

        self._spec["retry"] = {
            "max_attempts": max_attempts,
            "backoff": backoff,
            "base": base,
            "factor": factor,
        }
        return self

    def with_resources(self, **costs: int) -> "TaskBuilder":
        """Charge this task the given per-pool resource costs."""

        self._spec.setdefault("resources", {}).update(costs)
        return self

    def with_duration(self, ticks: int) -> "TaskBuilder":
        """Set how many virtual ticks a single attempt occupies."""

        self._spec["duration"] = ticks
        return self

    def with_metadata(self, **fields: Any) -> "TaskBuilder":
        """Attach free-form metadata to this task."""

        self._spec.setdefault("metadata", {}).update(fields)
        return self

    # Delegation so ``.task(...).task(...)`` and ``.build()`` chain through.
    def task(self, *args: Any, **kwargs: Any) -> "TaskBuilder":
        """Start a new task on the parent builder (chaining convenience)."""

        return self._parent.task(*args, **kwargs)

    def build(self) -> Dict[str, Any]:
        """Finish and return the whole pipeline config dict."""

        return self._parent.build()

    def load(self) -> Pipeline:
        """Finish and load the whole pipeline (chaining convenience)."""

        return self._parent.load()


class PipelineBuilder:
    """Assemble a pipeline config dict through chained calls.

    Construct with a pipeline name, optionally set a ``defaults`` block, then add
    tasks. Each :meth:`task` call appends a task spec and returns a
    :class:`TaskBuilder` for refining it; the parent builder is reachable through
    that handle so the whole thing chains. :meth:`build` validates referential
    integrity and returns the config dict.
    """

    def __init__(self, name: str = "pipeline") -> None:
        self._name = name
        self._defaults: Dict[str, Any] = {}
        self._tasks: List[Dict[str, Any]] = []
        self._ids: set = set()
        self._pools: Dict[str, int] = {}
        self._concurrency: Optional[int] = None

    def defaults(self, **fields: Any) -> "PipelineBuilder":
        """Merge ``fields`` into the pipeline-wide ``defaults`` block."""

        self._defaults.update(fields)
        return self

    def pool(self, name: str, capacity: int) -> "PipelineBuilder":
        """Declare a resource pool ``name`` with the given integer capacity."""

        self._pools[name] = capacity
        return self

    def concurrency(self, limit: int) -> "PipelineBuilder":
        """Set the global concurrency limit for the pipeline."""

        self._concurrency = limit
        return self

    def task(
        self,
        task_id: str,
        deps: Optional[List[str]] = None,
        priority: Optional[int] = None,
        action: Any = None,
        **extra: Any,
    ) -> TaskBuilder:
        """Add a task and return a :class:`TaskBuilder` to refine it.

        Rejects a duplicate id immediately so the mistake surfaces at the call
        site rather than deep inside ``load_pipeline``. Any recognised task field
        may be passed as a keyword (``duration``, ``timeout``, ``retry``,
        ``resources``, ``metadata``).
        """

        if task_id in self._ids:
            raise ConfigError("duplicate task id: {!r}".format(task_id))
        self._ids.add(task_id)
        spec: Dict[str, Any] = {"id": task_id}
        if deps:
            spec["deps"] = list(deps)
        if priority is not None:
            spec["priority"] = priority
        if action is not None:
            spec["action"] = action
        spec.update(extra)
        self._tasks.append(spec)
        return TaskBuilder(self, spec)

    def _validate_refs(self) -> None:
        """Raise if any task depends on an id that was never added."""

        for spec in self._tasks:
            for dep in spec.get("deps", []):
                if dep not in self._ids:
                    raise ConfigError(
                        "task {!r} depends on unknown task {!r}".format(
                            spec["id"], dep
                        )
                    )

    def build(self) -> Dict[str, Any]:
        """Return the assembled config dict, after checking dependency refs."""

        self._validate_refs()
        config: Dict[str, Any] = {"name": self._name, "tasks": list(self._tasks)}
        if self._defaults:
            config["defaults"] = dict(self._defaults)
        if self._pools:
            config["pools"] = dict(self._pools)
        if self._concurrency is not None:
            config["concurrency"] = self._concurrency
        return config

    def load(self) -> Pipeline:
        """Build the config and load it into a :class:`~taskflow.model.Pipeline`.

        Imported lazily so the builder module has no import-time dependency on the
        config loader (keeping the dependency graph shallow).
        """

        from taskflow.config import load_pipeline

        return load_pipeline(self.build())

    def __len__(self) -> int:
        return len(self._tasks)

    def __repr__(self) -> str:
        return "PipelineBuilder(name={!r}, tasks={})".format(
            self._name, len(self._tasks)
        )


def linear(name: str, *task_ids: str) -> Dict[str, Any]:
    """Build a straight-line pipeline where each task depends on the previous.

    A convenience for the common "do these steps in order" case::

        linear("nightly", "backup", "vacuum", "report")

    yields a three-task chain ``backup -> vacuum -> report``.
    """

    builder = PipelineBuilder(name)
    previous: Optional[str] = None
    for task_id in task_ids:
        deps = [previous] if previous is not None else None
        builder.task(task_id, deps=deps)
        previous = task_id
    return builder.build()


def fan_out(
    name: str, root: str, leaves: List[str], join: Optional[str] = None
) -> Dict[str, Any]:
    """Build a fan-out (and optional fan-in) pipeline.

    ``root`` runs first; every id in ``leaves`` depends on it and can run in
    parallel. If ``join`` is given, a final task by that name depends on every
    leaf, producing the classic diamond/scatter-gather shape.
    """

    builder = PipelineBuilder(name)
    builder.task(root)
    for leaf in leaves:
        builder.task(leaf, deps=[root])
    if join is not None:
        builder.task(join, deps=list(leaves))
    return builder.build()
