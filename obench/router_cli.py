"""CLI for Gateway Tax experiments."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import results, router_publish, router_report, router_run, router_spec


def _tasks_dir(value: str | None) -> str:
    return value or "tasks"


def _print_error(exc: Exception) -> int:
    print(f"obench router: {exc}", file=sys.stderr)
    return 2


def _validate(args: argparse.Namespace) -> int:
    experiment, tasks = router_run.validate_experiment(
        args.experiment, tasks_dir=_tasks_dir(args.tasks_dir)
    )
    print(
        f"valid experiment={experiment.experiment_id} "
        f"digest={experiment.digest} tasks={len(tasks)} arms={len(experiment.arms)}"
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    report = router_run.doctor_experiment(
        args.experiment, tasks_dir=_tasks_dir(args.tasks_dir)
    )
    print(json.dumps(report, sort_keys=True))
    if not report["usd_cap_enforceable"]:
        print(
            f"obench router: {router_run.FROZEN_PRICES_ENV} is required to run",
            file=sys.stderr,
        )
        return 2
    return 0


def _run(args: argparse.Namespace) -> int:
    experiment = router_spec.load_experiment(args.experiment)
    results_path = args.results or f"results/router-{experiment.experiment_id}.jsonl"
    summary = router_run.run_experiment(
        args.experiment,
        results_path=results_path,
        tasks_dir=_tasks_dir(args.tasks_dir),
        exec_mode=args.exec_mode,
        force=args.force,
    )
    print(
        f"results={summary.results_path} rows_appended={summary.rows_appended} "
        f"blocks_completed={summary.blocks_completed} "
        f"blocks_replaced={summary.blocks_replaced} "
        f"blocks_skipped={summary.blocks_skipped}"
    )
    return 0


def _report(args: argparse.Namespace) -> int:
    state = results.read_jsonl_for_resume(Path(args.results))
    if not state.rows:
        raise router_run.RouterRunError("results file contains no rows")
    expected = set()
    for row in state.rows:
        raw = row.get("expected_arm_ids")
        if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            expected.update(raw)
    report = router_report.aggregate(
        state.rows,
        expected_arm_ids=expected or None,
    )
    if args.json:
        print(json.dumps(report, allow_nan=False, sort_keys=True))
    else:
        print(router_report.render_text(report))
    return 0


def _publish(args: argparse.Namespace) -> int:
    results_path = Path(args.results).resolve()
    experiment = router_spec.load_experiment(args.experiment)
    state = results.read_jsonl_for_resume(results_path)
    if not state.rows:
        raise router_run.RouterRunError("results file contains no rows")
    price_snapshot = router_run.load_persisted_price_snapshot(results_path)
    ledger_dir = (
        Path(args.ledgers_dir).resolve()
        if args.ledgers_dir
        else results_path.parent / f".{results_path.stem}.router-ledgers"
    )
    ledgers = {}
    for row in state.rows:
        seal = row.get("ledger_seal")
        if not isinstance(seal, dict) or not isinstance(seal.get("ledger_file"), str):
            raise router_run.RouterRunError("result row is missing its sealed ledger binding")
        ledgers[results.result_cell_id(row)] = ledger_dir / seal["ledger_file"]
    provenance = router_publish.publish_bundle(
        results_path,
        args.bundle,
        experiment=experiment,
        policy=router_run.policy_snapshot(),
        catalog=router_run.catalog_snapshot(experiment),
        prices=price_snapshot,
        ledgers=ledgers,
    )
    print(json.dumps(provenance, sort_keys=True))
    return 0


def _verify(args: argparse.Namespace) -> int:
    print(json.dumps(router_publish.verify_bundle(args.bundle), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obench router",
        description="Run and report Gateway Tax experiments.",
    )
    sub = parser.add_subparsers(dest="router_command", required=True)

    validate = sub.add_parser("validate", help="validate experiment and task inputs")
    validate.add_argument("experiment")
    validate.add_argument("--tasks-dir")
    validate.set_defaults(handler=_validate)

    doctor = sub.add_parser("doctor", help="check credentials, Pi, tasks, and prices")
    doctor.add_argument("experiment")
    doctor.add_argument("--tasks-dir")
    doctor.set_defaults(handler=_doctor)

    run = sub.add_parser("run", help="run or resume an experiment")
    run.add_argument("experiment")
    run.add_argument("--results")
    run.add_argument("--tasks-dir")
    run.add_argument("--exec", dest="exec_mode", choices=("local", "docker"))
    run.add_argument("--force", action="store_true")
    run.set_defaults(handler=_run)

    report = sub.add_parser("report", help="report schema-v2 router results")
    report.add_argument("results")
    report.add_argument("--json", action="store_true")
    report.set_defaults(handler=_report)

    publish = sub.add_parser("publish", help="create a sanitized evidence bundle")
    publish.add_argument("results")
    publish.add_argument("experiment")
    publish.add_argument("bundle")
    publish.add_argument("--ledgers-dir")
    publish.set_defaults(handler=_publish)

    verify = sub.add_parser("verify", help="verify a Gateway Bench evidence bundle")
    verify.add_argument("bundle")
    verify.set_defaults(handler=_verify)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (
        OSError,
        results.ResultError,
        results.ResultsLogError,
        router_report.RouterReportError,
        router_publish.RouterPublishError,
        router_run.RouterRunError,
        router_spec.RouterSpecError,
    ) as exc:
        return _print_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())
