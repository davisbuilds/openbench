"""Schema validation for pipeline configuration dictionaries.

:func:`taskflow.config.load_pipeline` validates *just enough* to build a
:class:`~taskflow.model.Pipeline`, and it fails fast on the first problem by
raising :class:`~taskflow.model.ConfigError`. That is the right behaviour for the
run path, but authoring tools and CLIs want the opposite: check a whole config
and report *every* problem at once, with a severity and a path to the offending
field, so a user can fix them all in one pass.

This module provides that. :func:`validate_config` walks a config dict against
the same rules ``config.py`` enforces (plus a few advisory ones) and returns a
:class:`ValidationReport` -- a collection of :class:`Issue` objects, each with a
severity (:data:`ERROR` or :data:`WARNING`), a dotted location such as
``"tasks[2].retry.max_attempts"``, and a message. Nothing here raises on a bad
config; it *describes* it. A caller who wants the fail-fast behaviour can call
:meth:`ValidationReport.raise_if_errors`.

The validator is intentionally independent of ``load_pipeline`` so the two
cannot drift into disagreeing silently: this module re-derives the structural
rules and can be read as an executable specification of a valid config.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from taskflow.model import ConfigError
from taskflow.retry import _BACKOFF_KINDS

ERROR = "error"
WARNING = "warning"


class Issue:
    """A single validation finding at a located point in the config.

    Parameters
    ----------
    severity:
        Either :data:`ERROR` (the config would not build or run correctly) or
        :data:`WARNING` (it will build but something looks suspect).
    location:
        A dotted/bracketed path to the offending value, e.g.
        ``"tasks[0].deps"`` or ``"defaults.retry.backoff"``.
    message:
        A human-readable description of the problem.
    """

    __slots__ = ("severity", "location", "message")

    def __init__(self, severity: str, location: str, message: str) -> None:
        self.severity = severity
        self.location = location
        self.message = message

    def is_error(self) -> bool:
        """Return ``True`` if this issue is an :data:`ERROR`."""

        return self.severity == ERROR

    def __repr__(self) -> str:
        return "{}: {}: {}".format(
            self.severity.upper(), self.location, self.message
        )


class ValidationReport:
    """An ordered collection of :class:`Issue` objects from one validation.

    Aggregates every finding so a caller can inspect them all, filter by
    severity, or escalate to an exception. The report is truthy when it holds no
    errors (warnings alone do not make a config invalid), so ``if report:`` reads
    as "if the config is valid".
    """

    def __init__(self, issues: Optional[List[Issue]] = None) -> None:
        self._issues: List[Issue] = list(issues) if issues else []

    def add(self, severity: str, location: str, message: str) -> Issue:
        """Append a new issue and return it."""

        issue = Issue(severity, location, message)
        self._issues.append(issue)
        return issue

    def error(self, location: str, message: str) -> Issue:
        """Append an :data:`ERROR` issue."""

        return self.add(ERROR, location, message)

    def warning(self, location: str, message: str) -> Issue:
        """Append a :data:`WARNING` issue."""

        return self.add(WARNING, location, message)

    def issues(self) -> List[Issue]:
        """Return every issue, in the order it was found."""

        return list(self._issues)

    def errors(self) -> List[Issue]:
        """Return only the :data:`ERROR` issues."""

        return [i for i in self._issues if i.severity == ERROR]

    def warnings(self) -> List[Issue]:
        """Return only the :data:`WARNING` issues."""

        return [i for i in self._issues if i.severity == WARNING]

    def is_valid(self) -> bool:
        """Return ``True`` if there are no errors (warnings are allowed)."""

        return not self.errors()

    def raise_if_errors(self) -> None:
        """Raise :class:`~taskflow.model.ConfigError` if any error was found.

        The raised error's message lists every error issue, joining the
        fail-fast contract of ``load_pipeline`` with the collect-everything
        behaviour of the validator: you see all the problems, but still get an
        exception when you asked for one.
        """

        errs = self.errors()
        if errs:
            joined = "; ".join(
                "{}: {}".format(i.location, i.message) for i in errs
            )
            raise ConfigError("invalid pipeline config: {}".format(joined))

    def merge(self, other: "ValidationReport") -> "ValidationReport":
        """Append another report's issues into this one and return ``self``."""

        self._issues.extend(other.issues())
        return self

    def __bool__(self) -> bool:
        return self.is_valid()

    def __len__(self) -> int:
        return len(self._issues)

    def __iter__(self):
        return iter(self._issues)

    def __repr__(self) -> str:
        return "ValidationReport(errors={}, warnings={})".format(
            len(self.errors()), len(self.warnings())
        )


def _validate_retry(
    retry: Any, location: str, report: ValidationReport
) -> None:
    """Validate a ``retry`` sub-block, appending issues to ``report``."""

    if not isinstance(retry, dict):
        report.error(location, "retry must be a mapping")
        return
    max_attempts = retry.get("max_attempts")
    if max_attempts is not None:
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
            report.error(
                location + ".max_attempts", "max_attempts must be an integer"
            )
        elif max_attempts < 1:
            report.error(
                location + ".max_attempts", "max_attempts must be >= 1"
            )
    backoff = retry.get("backoff")
    if backoff is not None and backoff not in _BACKOFF_KINDS:
        report.error(
            location + ".backoff",
            "unknown backoff {!r}; expected one of {}".format(
                backoff, sorted(_BACKOFF_KINDS)
            ),
        )
    base = retry.get("base")
    if base is not None and (not isinstance(base, int) or base < 0):
        report.error(location + ".base", "base must be a non-negative integer")
    factor = retry.get("factor")
    if factor is not None and (not isinstance(factor, int) or factor < 1):
        report.error(location + ".factor", "factor must be an integer >= 1")


def _validate_resources(
    resources: Any, location: str, report: ValidationReport
) -> None:
    """Validate a ``resources`` cost mapping, appending issues to ``report``."""

    if not isinstance(resources, dict):
        report.error(location, "resources must be a mapping")
        return
    for pool, cost in resources.items():
        loc = "{}.{}".format(location, pool)
        if not isinstance(cost, int) or isinstance(cost, bool) or cost < 0:
            report.error(loc, "resource cost must be a non-negative integer")


def _validate_task(
    task: Any,
    index: int,
    seen_ids: Dict[str, int],
    report: ValidationReport,
) -> Optional[str]:
    """Validate one task dict; return its id if usable, else ``None``."""

    base = "tasks[{}]".format(index)
    if not isinstance(task, dict):
        report.error(base, "each task must be a mapping")
        return None

    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        report.error(base + ".id", "task id must be a non-empty string")
        task_id = None
    elif task_id in seen_ids:
        report.error(
            base + ".id",
            "duplicate task id {!r} (first seen at tasks[{}])".format(
                task_id, seen_ids[task_id]
            ),
        )
    else:
        seen_ids[task_id] = index

    deps = task.get("deps", [])
    if deps and not isinstance(deps, list):
        report.error(base + ".deps", "deps must be a list")
    elif isinstance(deps, list):
        for j, dep in enumerate(deps):
            if not isinstance(dep, str) or not dep:
                report.error(
                    "{}.deps[{}]".format(base, j),
                    "dependency must be a non-empty string",
                )

    priority = task.get("priority")
    if priority is not None and (
        not isinstance(priority, int) or isinstance(priority, bool)
    ):
        report.error(base + ".priority", "priority must be an integer")

    duration = task.get("duration")
    if duration is not None and (
        not isinstance(duration, int) or duration < 1
    ):
        report.error(base + ".duration", "duration must be an integer >= 1")

    if "retry" in task:
        _validate_retry(task["retry"], base + ".retry", report)
    if "resources" in task:
        _validate_resources(task["resources"], base + ".resources", report)

    if task.get("action") is None:
        report.warning(
            base + ".action",
            "task has no action; it will be treated as a no-op",
        )
    return task_id


def validate_config(config: Any) -> ValidationReport:
    """Validate a whole pipeline config dict and return a full report.

    Runs every structural check ``load_pipeline`` performs -- top-level shape,
    per-task fields, retry and resource sub-blocks, duplicate ids and dangling
    dependencies -- but collects *all* findings rather than stopping at the
    first. Also emits advisory warnings (e.g. a task with no action). The
    returned :class:`ValidationReport` is falsy exactly when there is at least
    one error.
    """

    report = ValidationReport()
    if not isinstance(config, dict):
        report.error("<root>", "pipeline config must be a mapping")
        return report

    if "tasks" not in config:
        report.error("tasks", "pipeline config must contain a 'tasks' list")
    elif not isinstance(config["tasks"], list):
        report.error("tasks", "'tasks' must be a list")

    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        report.error("defaults", "'defaults' must be a mapping")
    else:
        if "retry" in defaults:
            _validate_retry(defaults["retry"], "defaults.retry", report)
        if "resources" in defaults:
            _validate_resources(
                defaults["resources"], "defaults.resources", report
            )

    tasks = config.get("tasks")
    if not isinstance(tasks, list):
        return report

    seen_ids: Dict[str, int] = {}
    for index, task in enumerate(tasks):
        _validate_task(task, index, seen_ids, report)

    # Dangling-dependency check across all valid ids.
    known = set(seen_ids)
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        for j, dep in enumerate(task.get("deps", []) or []):
            if isinstance(dep, str) and dep and dep not in known:
                report.error(
                    "tasks[{}].deps[{}]".format(index, j),
                    "dependency {!r} names no known task".format(dep),
                )

    _validate_pools(config, report)
    return report


def _validate_pools(config: Dict[str, Any], report: ValidationReport) -> None:
    """Validate the optional top-level ``pools`` and ``concurrency`` blocks."""

    pools = config.get("pools")
    if pools is not None:
        if not isinstance(pools, dict):
            report.error("pools", "'pools' must be a mapping")
        else:
            for name, capacity in pools.items():
                if (
                    not isinstance(capacity, int)
                    or isinstance(capacity, bool)
                    or capacity < 0
                ):
                    report.error(
                        "pools.{}".format(name),
                        "pool capacity must be a non-negative integer",
                    )

    concurrency = config.get("concurrency")
    if concurrency is not None and (
        not isinstance(concurrency, int)
        or isinstance(concurrency, bool)
        or concurrency < 1
    ):
        report.error("concurrency", "concurrency must be an integer >= 1")


def is_valid(config: Any) -> bool:
    """Return ``True`` if ``config`` validates with no errors."""

    return validate_config(config).is_valid()
