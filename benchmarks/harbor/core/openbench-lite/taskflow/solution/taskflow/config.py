"""Build a :class:`~taskflow.model.Pipeline` from a plain Python dictionary.

A pipeline configuration is an ordinary ``dict`` with this shape::

    {
        "name": "etl",
        "defaults": {
            "priority": 0,
            "timeout": 30,
            "retry": {"max_attempts": 1, "backoff": "constant", "base": 0},
            "resources": {},
        },
        "tasks": [
            {"id": "extract", "action": extract_fn},
            {
                "id": "transform",
                "action": transform_fn,
                "deps": ["extract"],
                "priority": 5,
                "retry": {"max_attempts": 3},
            },
            ...
        ],
    }

The ``defaults`` block is *deep-merged* into every task so that a task only has
to spell out what differs from the pipeline-wide defaults. Nested blocks such
as ``retry`` and ``resources`` merge key-by-key: a task that overrides only
``retry.max_attempts`` still inherits the default ``retry.backoff``.

:func:`load_pipeline` validates the structure and returns a fully-populated
:class:`~taskflow.model.Pipeline`; it never executes any task action.
"""

from __future__ import annotations

from taskflow.model import ConfigError, Pipeline, Task
from taskflow.retry import RetryPolicy

# Fields a task dict may carry that are *not* merged from defaults verbatim
# (they are structural, not scheduling knobs).
_STRUCTURAL_FIELDS = frozenset({"id", "action", "deps"})

# The scheduling knobs a task inherits from ``defaults`` when it does not set
# them itself. Nested mapping fields (retry, resources) are deep-merged.
_SCALAR_DEFAULT_FIELDS = ("priority", "timeout", "duration")
_MAPPING_DEFAULT_FIELDS = ("retry", "resources")


def deep_merge(base, override):
    """Return a new dict: ``base`` with ``override`` applied on top.

    The merge is recursive. When the same key holds a mapping in *both*
    ``base`` and ``override``, the two mappings are merged key-by-key so that
    ``override`` wins at every individual leaf while values only present in
    ``base`` are preserved. For every other key, ``override`` replaces
    ``base`` outright. Neither input is mutated.

    Example::

        deep_merge(
            {"retry": {"max_attempts": 1, "backoff": "constant"}},
            {"retry": {"max_attempts": 3}},
        )
        # -> {"retry": {"max_attempts": 3, "backoff": "constant"}}
    """

    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            # Recurse with (base_child, override_child) so the override still
            # wins at the leaves while the base fills in the untouched keys.
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_top_level(config):
    if not isinstance(config, dict):
        raise ConfigError("pipeline config must be a mapping")
    if "tasks" not in config:
        raise ConfigError("pipeline config must contain a 'tasks' list")
    if not isinstance(config["tasks"], list):
        raise ConfigError("'tasks' must be a list")
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("'defaults' must be a mapping")
    return defaults


def _effective_task_config(defaults, task_dict):
    """Return the task dict with pipeline defaults deep-merged underneath it.

    The task's own values take precedence; anything it omits is filled in from
    ``defaults``. Structural fields (id/action/deps) are copied across
    untouched because they never come from defaults.
    """

    # Split the task dict into the structural part and the scheduling part.
    scheduling = {
        k: v for k, v in task_dict.items() if k not in _STRUCTURAL_FIELDS
    }
    merged = deep_merge(defaults, scheduling)
    for field in _STRUCTURAL_FIELDS:
        if field in task_dict:
            merged[field] = task_dict[field]
    return merged


def _build_task(merged):
    """Construct a :class:`Task` from an already-merged config dict."""

    if "id" not in merged:
        raise ConfigError("every task must have an 'id'")
    task_id = merged["id"]
    if not isinstance(task_id, str) or not task_id:
        raise ConfigError("task id must be a non-empty string")

    deps = merged.get("deps", [])
    if deps and not isinstance(deps, list):
        raise ConfigError("task {!r} 'deps' must be a list".format(task_id))

    retry_cfg = merged.get("retry")
    retry_policy = RetryPolicy.from_dict(retry_cfg)

    resources = merged.get("resources", {})
    if resources and not isinstance(resources, dict):
        raise ConfigError(
            "task {!r} 'resources' must be a mapping".format(task_id)
        )
    for pool, cost in (resources or {}).items():
        if not isinstance(cost, int) or cost < 0:
            raise ConfigError(
                "task {!r} resource {!r} cost must be a non-negative "
                "integer".format(task_id, pool)
            )

    priority = merged.get("priority", 0)
    if not isinstance(priority, int):
        raise ConfigError(
            "task {!r} priority must be an integer".format(task_id)
        )

    duration = merged.get("duration", 1)
    timeout = merged.get("timeout")

    return Task(
        task_id,
        action=merged.get("action"),
        deps=deps,
        priority=priority,
        retry_policy=retry_policy,
        resource_costs=resources or {},
        timeout=timeout,
        duration=duration,
        metadata=merged.get("metadata"),
    )


def load_pipeline(config):
    """Validate ``config`` and return a populated :class:`Pipeline`.

    Steps:

    1. Validate the top-level shape (``tasks`` list, ``defaults`` mapping).
    2. For each task, deep-merge the defaults underneath the task's own
       settings, so per-task overrides win and omitted knobs are inherited.
    3. Construct a :class:`Task`, validating each field.
    4. Reject duplicate ids and dependencies that name unknown tasks.

    Raises :class:`ConfigError` on any structural problem.
    """

    defaults = _validate_top_level(config)
    name = config.get("name", "pipeline")

    pipeline = Pipeline(name=name, defaults=defaults)
    for task_dict in config["tasks"]:
        if not isinstance(task_dict, dict):
            raise ConfigError("each task must be a mapping")
        merged = _effective_task_config(defaults, task_dict)
        pipeline.add(_build_task(merged))

    # Validate the dependency references now that every task is known.
    known = set(pipeline.task_ids())
    for task in pipeline.tasks():
        for dep in task.deps:
            if dep not in known:
                raise ConfigError(
                    "task {!r} depends on unknown task {!r}".format(
                        task.id, dep
                    )
                )
    return pipeline
