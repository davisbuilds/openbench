"""CLI for request-level Gateway Probe experiments."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import (
    gateway_probe_publish,
    gateway_probe_report,
    gateway_probe_results,
    gateway_probe_run,
    gateway_probe_spec,
    gateway_run,
    gateway_spec,
)
from .gateway_probe_models import GatewayProbeRunError


def _positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a positive integer"
        ) from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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
        allow_cost_unavailable_block_recovery=(
            args.allow_cost_unavailable_block_recovery
        ),
        max_blocks=args.max_blocks,
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


def _publish(args: argparse.Namespace) -> int:
    manifest = gateway_probe_publish.publish_bundle(
        args.run_dir,
        args.bundle_dir,
        verified_with_commit=args.verified_with_commit,
    )
    print(
        f"published={Path(args.bundle_dir).resolve()} "
        f"rows={manifest['result_count']} "
        f"blocks=cold:{manifest['complete_blocks']['cold']}/"
        f"{manifest['scheduled_blocks_per_condition']},"
        f"warm:{manifest['complete_blocks']['warm']}/"
        f"{manifest['scheduled_blocks_per_condition']} "
        f"verified_with_commit="
        f"{manifest['verification']['verified_with_commit']}"
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    manifest = gateway_probe_publish.verify_bundle(args.bundle_dir)
    print(
        f"verified={Path(args.bundle_dir).resolve()} "
        f"rows={manifest['result_count']} "
        f"blocks=cold:{manifest['complete_blocks']['cold']}/"
        f"{manifest['scheduled_blocks_per_condition']},"
        f"warm:{manifest['complete_blocks']['warm']}/"
        f"{manifest['scheduled_blocks_per_condition']} "
        f"verified_with_commit="
        f"{manifest['verification']['verified_with_commit']}"
    )
    return 0


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _write_or_verify(path: Path, content: bytes, label: str) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise GatewayProbeRunError(
                f"{label} snapshot does not match existing {path}"
            )
        return
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _write_atomic(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remove_generated_bundle_files(output_dir: Path) -> None:
    for name in ("report.md", "report.json", "manifest.json"):
        try:
            (output_dir / name).unlink()
        except FileNotFoundError:
            pass


def _utc_stamp() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .strftime("%Y%m%dT%H%M%S%fZ")
    )


def _price_snapshot_env(snapshot: Any) -> str:
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {
            "schema_version", "price_id", "currency", "prices",
        }
        or snapshot.get("schema_version") != 1
        or snapshot.get("price_id") != "frozen-env-v1"
        or snapshot.get("currency") != "USD"
        or not isinstance(snapshot.get("prices"), list)
        or not snapshot["prices"]
    ):
        raise GatewayProbeRunError("frozen price snapshot is malformed")
    prices = {}
    for item in snapshot["prices"]:
        if (
            not isinstance(item, dict)
            or set(item) != {
                "model", "input_per_million", "output_per_million",
                "effective_at", "currency",
            }
            or item.get("currency") != "USD"
            or not isinstance(item.get("model"), str)
            or item["model"] in prices
        ):
            raise GatewayProbeRunError("frozen price snapshot is malformed")
        prices[item["model"]] = {
            "input_per_million": item.get("input_per_million"),
            "output_per_million": item.get("output_per_million"),
            "effective_at": item.get("effective_at"),
        }
    return json.dumps(
        prices,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _bundle_manifest(
    *,
    output_dir: Path,
    experiment: gateway_probe_spec.GatewayProbeExperiment,
) -> dict[str, Any]:
    names = (
        "experiment.toml",
        "prices.json",
        "results.jsonl",
        "report.md",
        "report.json",
    )
    return {
        "schema_version": 1,
        "benchmark": gateway_probe_results.BENCHMARK,
        "result_schema_version": gateway_probe_results.RESULT_SCHEMA_VERSION,
        "experiment_id": experiment.experiment_id,
        "experiment_digest": experiment.digest,
        "files": {
            name: {"sha256": _sha256(output_dir / name)}
            for name in names
        },
    }


def _benchmark(args: argparse.Namespace) -> int:
    experiment_path = Path(args.experiment).resolve()
    experiment = gateway_probe_spec.load_experiment(experiment_path)
    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else Path(
            "results",
            f"gateway-probe-{experiment.experiment_id}-{_utc_stamp()}",
        ).resolve()
    )
    if not args.output_dir and output_dir.exists():
        raise GatewayProbeRunError(
            f"default artifact directory already exists: {output_dir}"
        )
    prices_path = output_dir / "prices.json"
    run_environ = dict(os.environ)
    if prices_path.exists():
        try:
            stored_prices = json.loads(prices_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GatewayProbeRunError(
                f"frozen price snapshot is invalid: {prices_path}"
            ) from exc
        run_environ[gateway_probe_run.FROZEN_PRICES_ENV] = (
            _price_snapshot_env(stored_prices)
        )
    doctor = gateway_probe_run.doctor_experiment(
        experiment_path,
        environ=run_environ,
    )
    if not doctor["ok"]:
        missing = []
        if doctor["missing_auth_envs"]:
            missing.append(
                "auth=" + ",".join(doctor["missing_auth_envs"])
            )
        if doctor["missing_price_models"]:
            missing.append(
                "prices=" + ",".join(doctor["missing_price_models"])
            )
        raise GatewayProbeRunError(
            "doctor failed: " + "; ".join(missing)
        )
    _prices, price_snapshot = gateway_run.load_frozen_prices(run_environ)
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / "experiment.toml"
    _write_or_verify(
        snapshot_path,
        experiment_path.read_bytes(),
        "experiment",
    )
    _write_or_verify(
        prices_path,
        _canonical_json(price_snapshot).encode("ascii"),
        "frozen price",
    )
    _remove_generated_bundle_files(output_dir)
    results_path = output_dir / "results.jsonl"
    summary = gateway_probe_run.run_experiment(
        snapshot_path,
        results_path=results_path,
        environ=run_environ,
        force=args.force,
        allow_cost_unavailable_block_recovery=(
            args.allow_cost_unavailable_block_recovery
        ),
        max_blocks=args.max_blocks,
    )
    rows = gateway_probe_results.load_results(results_path)
    report = gateway_probe_report.aggregate(rows, experiment=experiment)
    report_json = _canonical_json(report)
    report_markdown = gateway_probe_report.render_text(report) + "\n"
    _write_atomic(output_dir / "report.json", report_json)
    _write_atomic(output_dir / "report.md", report_markdown)
    manifest = _bundle_manifest(
        output_dir=output_dir,
        experiment=experiment,
    )
    _write_atomic(output_dir / "manifest.json", _canonical_json(manifest))
    print(
        f"artifacts={output_dir} results={results_path} "
        f"rows_appended={summary.rows_appended} "
        f"blocks_completed={summary.blocks_completed} "
        f"blocks_replaced={summary.blocks_replaced} "
        f"blocks_skipped={summary.blocks_skipped}"
    )
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
    run.add_argument(
        "--max-blocks",
        type=_positive_integer,
        help="process at most this many complete matched blocks",
    )
    run_mode = run.add_mutually_exclusive_group()
    run_mode.add_argument("--force", action="store_true")
    run_mode.add_argument(
        "--allow-cost-unavailable-block-recovery",
        action="store_true",
        help=(
            "resume past complete cost-unavailable blocks and finish only the "
            "current block after a new cost-unavailable stop"
        ),
    )
    run.set_defaults(handler=_run)
    report = sub.add_parser("report", help="report probe results")
    report.add_argument("results")
    report.add_argument("--json", action="store_true")
    report.set_defaults(handler=_report)
    benchmark = sub.add_parser(
        "benchmark",
        help="doctor, run/resume, and write a reproducible artifact bundle",
    )
    benchmark.add_argument("experiment")
    benchmark.add_argument("--output-dir")
    benchmark.add_argument(
        "--max-blocks",
        type=_positive_integer,
        help="process at most this many complete matched blocks",
    )
    benchmark_mode = benchmark.add_mutually_exclusive_group()
    benchmark_mode.add_argument("--force", action="store_true")
    benchmark_mode.add_argument(
        "--allow-cost-unavailable-block-recovery",
        action="store_true",
        help=(
            "resume past complete cost-unavailable blocks and finish only the "
            "current block after a new cost-unavailable stop"
        ),
    )
    benchmark.set_defaults(handler=_benchmark)
    publish = sub.add_parser(
        "publish",
        help="project a private run into a public, verified probe bundle",
    )
    publish.add_argument("run_dir")
    publish.add_argument("bundle_dir")
    publish.add_argument("--verified-with-commit")
    publish.set_defaults(handler=_publish)
    verify = sub.add_parser(
        "verify",
        help="verify a public probe bundle and recompute its report",
    )
    verify.add_argument("bundle_dir")
    verify.set_defaults(handler=_verify)
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
