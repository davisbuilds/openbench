"""A command-line front-end for loading and inspecting pipelines.

This module turns the library into a small tool. It reads a pipeline definition
from a JSON file, then -- depending on the sub-command -- validates it, renders
its dependency graph, computes a dry-run execution plan, or actually runs it and
prints a report.

Because a JSON file cannot carry Python callables, tasks loaded this way have no
action and therefore behave as no-ops (they always succeed). That is exactly what
the structural sub-commands (``validate``, ``graph``, ``plan``) want, and it
makes ``run`` a faithful demonstration of the scheduling machinery on the graph's
shape. A JSON pipeline may use the same ``${...}`` templating that
:mod:`taskflow.templating` understands, rendered from ``--param NAME=VALUE``
arguments before loading.

The entry point is :func:`main`, which takes an argv list (defaulting to
``sys.argv``) and returns a process exit code, so it is equally usable as
``python -m`` glue or from tests. It prints to caller-supplied streams to stay
testable and never calls :func:`sys.exit` itself except through the thin
``__main__`` guard.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, TextIO

from taskflow import reporting, templating
from taskflow.config import load_pipeline
from taskflow.dag import Dag
from taskflow.planner import plan_pipeline
from taskflow.runner import run_pipeline
from taskflow.validation import validate_config


def _coerce_param(raw: str) -> Any:
    """Interpret a ``--param`` value, trying JSON then falling back to string.

    So ``--param count=3`` yields the integer ``3`` and ``--param
    shards=[\"a\",\"b\"]`` yields a list, while ``--param name=etl`` yields the
    plain string ``"etl"``. This lets templating receive correctly-typed values
    from the command line.
    """

    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _parse_params(pairs: Optional[List[str]]) -> Dict[str, Any]:
    """Parse ``NAME=VALUE`` strings into a params mapping for templating."""

    params: Dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(
                "--param expects NAME=VALUE, got {!r}".format(pair)
            )
        key, _, value = pair.partition("=")
        params[key] = _coerce_param(value)
    return params


def _load_config(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Read a JSON config from ``path`` and render its templating."""

    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return templating.render(raw, params)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser with its sub-commands."""

    parser = argparse.ArgumentParser(
        prog="taskflow",
        description="Load, inspect and run taskflow pipelines from JSON.",
    )
    parser.add_argument(
        "--param",
        action="append",
        metavar="NAME=VALUE",
        help="a templating parameter; may be given several times",
    )
    sub = parser.add_subparsers(dest="command")

    for name, help_text in (
        ("validate", "validate the config and report every issue"),
        ("graph", "print the dependency graph"),
        ("plan", "compute and print a dry-run execution plan"),
        ("run", "run the pipeline and print a report"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("config", help="path to a pipeline JSON file")
        if name == "plan" or name == "run":
            p.add_argument(
                "--concurrency",
                type=int,
                default=None,
                help="maximum concurrent running tasks",
            )
        if name in ("graph", "run", "plan"):
            p.add_argument(
                "--json",
                action="store_true",
                help="emit JSON instead of text where supported",
            )
        if name == "graph":
            p.add_argument(
                "--dot",
                action="store_true",
                help="emit Graphviz DOT instead of a text tree",
            )
    return parser


def _cmd_validate(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """Handle the ``validate`` sub-command."""

    config = _load_config(args.config, _parse_params(args.param))
    report = validate_config(config)
    for issue in report.issues():
        stream = err if issue.is_error() else out
        print(str(issue), file=stream)
    if report.is_valid():
        print("OK: config is valid ({} warning(s))".format(
            len(report.warnings())), file=out)
        return 0
    print("INVALID: {} error(s)".format(len(report.errors())), file=err)
    return 1


def _cmd_graph(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """Handle the ``graph`` sub-command."""

    config = _load_config(args.config, _parse_params(args.param))
    pipeline = load_pipeline(config)
    dag = Dag.from_pipeline(pipeline)
    if getattr(args, "dot", False):
        print(reporting.render_dag_dot(dag, pipeline.name), file=out)
    else:
        print(reporting.render_dag(dag), file=out)
    return 0


def _cmd_plan(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """Handle the ``plan`` sub-command."""

    config = _load_config(args.config, _parse_params(args.param))
    pipeline = load_pipeline(config)
    pools = config.get("pools") if isinstance(config, dict) else None
    plan = plan_pipeline(pipeline, concurrency=args.concurrency, pools=pools)
    if getattr(args, "json", False):
        print(json.dumps(plan.summary(), indent=2, sort_keys=True), file=out)
    else:
        print(reporting.render_plan(plan), file=out)
    return 0


def _cmd_run(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """Handle the ``run`` sub-command."""

    config = _load_config(args.config, _parse_params(args.param))
    report = run_pipeline(config, concurrency=args.concurrency)
    if getattr(args, "json", False):
        print(reporting.render_json(report), file=out)
    else:
        print(reporting.render_report(report), file=out)
    return 0 if report.ok() else 1


_HANDLERS = {
    "validate": _cmd_validate,
    "graph": _cmd_graph,
    "plan": _cmd_plan,
    "run": _cmd_run,
}


def main(
    argv: Optional[List[str]] = None,
    out: Optional[TextIO] = None,
    err: Optional[TextIO] = None,
) -> int:
    """Parse ``argv`` and dispatch to the chosen sub-command.

    Returns a process exit code: 0 on success, 1 when a config is invalid or a
    run does not fully succeed, and 2 for a usage error. Streams default to
    stdout/stderr but may be injected for testing.
    """

    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help(err)
        return 2

    handler = _HANDLERS[args.command]
    try:
        return handler(args, out, err)
    except (OSError, ValueError, templating.TemplateError) as exc:
        print("error: {}".format(exc), file=err)
        return 2


if __name__ == "__main__":  # pragma: no cover - thin process glue
    raise SystemExit(main())
