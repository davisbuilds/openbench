"""CLI for request-level Gateway Probe experiments."""

from __future__ import annotations

import argparse
import json
import sys

from . import (
    gateway_probe_report,
    gateway_probe_results,
    gateway_probe_run,
    gateway_probe_spec,
    gateway_run,
    gateway_spec,
)
from .gateway_probe_models import GatewayProbeRunError


def _validate(args: argparse.Namespace) -> int:
    experiment = gateway_probe_run.validate_experiment(args.experiment)
    print(
        f"valid probe={experiment.experiment_id} digest={experiment.digest} "
        f"cases={len(experiment.cases)} arms={len(experiment.arms)}"
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    report = gateway_probe_run.doctor_experiment(args.experiment)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 2


def _run(args: argparse.Namespace) -> int:
    experiment = gateway_probe_spec.load_experiment(args.experiment)
    results_path = args.results or f"results/gateway-probe-{experiment.experiment_id}.jsonl"
    summary = gateway_probe_run.run_experiment(
        args.experiment,
        results_path=results_path,
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
    rows = gateway_probe_results.load_results(args.results)
    report = gateway_probe_report.aggregate(rows)
    if args.json:
        print(json.dumps(report, allow_nan=False, sort_keys=True))
    else:
        print(gateway_probe_report.render_text(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obench gateway probe",
        description="Measure fixed request routes through AI gateways.",
    )
    sub = parser.add_subparsers(dest="probe_command", required=True)
    validate = sub.add_parser("validate", help="validate a probe experiment")
    validate.add_argument("experiment")
    validate.set_defaults(handler=_validate)
    doctor = sub.add_parser("doctor", help="check credentials and frozen prices")
    doctor.add_argument("experiment")
    doctor.set_defaults(handler=_doctor)
    run = sub.add_parser("run", help="run or resume a probe experiment")
    run.add_argument("experiment")
    run.add_argument("--results")
    run.add_argument("--force", action="store_true")
    run.set_defaults(handler=_run)
    report = sub.add_parser("report", help="report probe results")
    report.add_argument("results")
    report.add_argument("--json", action="store_true")
    report.set_defaults(handler=_report)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (
        OSError,
        gateway_probe_spec.GatewayProbeSpecError,
        GatewayProbeRunError,
        gateway_probe_report.GatewayProbeReportError,
        gateway_run.GatewayRunError,
        gateway_spec.GatewaySpecError,
    ) as exc:
        print(f"obench gateway probe: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
