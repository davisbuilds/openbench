"""taskflow -- a deterministic, in-memory job-orchestration engine.

``taskflow`` runs a pipeline of interdependent tasks to completion on a virtual
clock, with retries, priority scheduling, resource-limited concurrency, and a
full event-sourced history -- all single-process, deterministic, and dependency
free.

The typical entry point is :func:`taskflow.runner.run_pipeline`, which takes a
plain configuration dictionary and returns a :class:`~taskflow.runner.RunReport`::

    from taskflow import run_pipeline

    report = run_pipeline({
        "name": "demo",
        "defaults": {"retry": {"max_attempts": 1}},
        "tasks": [
            {"id": "a", "action": lambda ctx: None},
            {"id": "b", "action": lambda ctx: None, "deps": ["a"]},
        ],
    })
    assert report.ok()

The individual building blocks (the DAG, the ready queue, the state machine,
the resource manager, the event bus, the history) are all importable from their
own submodules and usable in isolation.
"""

from taskflow.config import load_pipeline
from taskflow.dag import Dag
from taskflow.events import EventBus
from taskflow.history import History
from taskflow.model import (
    ConfigError,
    Event,
    GraphError,
    JobContext,
    JobRun,
    Pipeline,
    State,
    Task,
    TaskflowError,
)
from taskflow.queue import ReadyQueue
from taskflow.resources import ResourceError, ResourceManager, ResourcePool
from taskflow.retry import RetryPolicy
from taskflow.runner import RunReport, run_pipeline
from taskflow.scheduler import Scheduler, SchedulerResult
from taskflow.statemachine import IllegalTransition, StateMachine

__all__ = [
    "load_pipeline",
    "Dag",
    "EventBus",
    "History",
    "ConfigError",
    "Event",
    "GraphError",
    "JobContext",
    "JobRun",
    "Pipeline",
    "State",
    "Task",
    "TaskflowError",
    "ReadyQueue",
    "ResourceError",
    "ResourceManager",
    "ResourcePool",
    "RetryPolicy",
    "RunReport",
    "run_pipeline",
    "Scheduler",
    "SchedulerResult",
    "IllegalTransition",
    "StateMachine",
]
