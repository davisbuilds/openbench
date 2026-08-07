"""CLI integration for native Computer-Use planning, state, and reporting."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Callable, Mapping, Sequence

from .native_matrix import (
    NativeMatrixError,
    build_native_matrix,
    canonical_bytes,
    reconcile_native_state,
)
from .native_report import NativeReportError, build_native_report


EXIT_OK = 0
EXIT_ERROR = 2
EXIT_INCOMPLETE = 3
EXIT_NONCOMPARABLE = 4


class NativeCliError(ValueError):
    """Raised when native CLI input or immutable output is invalid."""


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(f"duplicate object key {key!r}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _read_json_object(path: str | os.PathLike[str], *, label: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise NativeCliError(f"{label} must not be a symlink: {source}")
    try:
        value = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise NativeCliError(f"cannot load {label} {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise NativeCliError(f"{label} must contain one JSON object: {source}")
    return value


def _read_results_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    source = Path(path).expanduser()
    if source.is_symlink():
        raise NativeCliError(f"results JSONL must not be a symlink: {source}")
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise NativeCliError(f"cannot load results JSONL {source}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise NativeCliError(
                f"invalid results JSONL {source}:{line_number}: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise NativeCliError(
                f"results JSONL {source}:{line_number} is not an object"
            )
        rows.append(row)
    return rows


def _open_output_parent(destination: Path, *, label: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    anchor = Path(destination.anchor)
    descriptor = os.open(anchor, flags)
    try:
        for part in destination.parent.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise NativeCliError(
                    f"{label} path has an unsafe parent component: {destination}"
                ) from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_at(directory_fd: int, name: str, *, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise NativeCliError(f"{label} path is unsafe or not a file: {name}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NativeCliError(f"{label} path is not a regular file: {name}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _write_immutable_json(
    path: str | os.PathLike[str],
    value: Mapping[str, Any],
    *,
    label: str,
) -> tuple[Path, str]:
    """Atomically create canonical JSON, allowing only identical existing bytes."""
    requested = Path(path).expanduser()
    if requested.suffix.lower() != ".json":
        raise NativeCliError(f"{label} path must end in .json")
    destination = Path(os.path.abspath(requested))
    if destination.name in {"", ".", ".."}:
        raise NativeCliError(f"{label} path is invalid: {requested}")
    payload = canonical_bytes(value) + b"\n"
    parent_fd = _open_output_parent(destination, label=label)
    temporary_name = (
        f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        try:
            existing = _read_regular_at(parent_fd, destination.name, label=label)
        except FileNotFoundError:
            pass
        else:
            if existing != payload:
                raise NativeCliError(
                    f"refusing to overwrite divergent {label}: {destination}"
                )
            return destination, "unchanged"

        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            try:
                existing = _read_regular_at(
                    parent_fd, destination.name, label=label
                )
            except FileNotFoundError:
                raise NativeCliError(
                    f"{label} destination changed during creation: {destination}"
                ) from None
            if existing != payload:
                raise NativeCliError(
                    f"refusing to overwrite divergent {label}: {destination}"
                ) from None
            status = "unchanged"
        else:
            status = "created"
        os.fsync(parent_fd)
        return destination, status
    finally:
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obench native",
        description="Experimental native macOS Computer-Use planning and execution.",
        epilog=(
            "Exit codes: 0 complete; 2 usage/input/output error; "
            "3 incomplete report written; 4 noncomparable report evidence."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser(
        "run", help="run and immediately import one explicit sealed native trial"
    )
    run_parser.add_argument("config", help="openbench.native-run.v0 TOML config")

    plan_parser = sub.add_parser(
        "plan", help="compile deterministic matched native comparison intent"
    )
    plan_parser.add_argument("spec", help="JSON plan specification")
    plan_parser.add_argument("--output", required=True, help="immutable plan JSON")

    state_parser = sub.add_parser(
        "state", help="reconcile immutable plan completion evidence"
    )
    state_parser.add_argument("plan", help="canonical plan JSON")
    state_parser.add_argument("--output", required=True, help="immutable state JSON")
    state_parser.add_argument(
        "--prior-state", help="prior canonical state JSON to extend"
    )
    state_parser.add_argument(
        "--observation",
        action="append",
        default=[],
        help="completed-cell observation JSON; repeat for multiple cells",
    )

    report_parser = sub.add_parser(
        "report", help="build a publication-safe matched native report"
    )
    report_parser.add_argument("plan", help="canonical plan JSON")
    report_parser.add_argument("--output", required=True, help="immutable report JSON")
    report_parser.add_argument(
        "--results",
        action="append",
        default=[],
        help="strict imported native results JSONL; repeatable",
    )
    report_parser.add_argument(
        "--bundle",
        action="append",
        default=[],
        help="validated native trial bundle directory; repeatable",
    )
    return parser


def _plan_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    spec = _read_json_object(args.spec, label="native plan specification")
    required = {"comparison_id", "task", "harness", "model", "arms"}
    allowed = required | {"repetitions"}
    missing = sorted(required - set(spec))
    extra = sorted(set(spec) - allowed)
    if missing or extra:
        raise NativeCliError(
            f"native plan specification fields disagree; missing={missing!r}, extra={extra!r}"
        )
    plan = build_native_matrix(
        comparison_id=spec["comparison_id"],
        task=spec["task"],
        harness=spec["harness"],
        model=spec["model"],
        arms=spec["arms"],
        repetitions=spec.get("repetitions", 5),
    )
    output, write_status = _write_immutable_json(
        args.output, plan, label="native plan"
    )
    return EXIT_OK, {
        "command": "plan",
        "output": str(output),
        "plan_sha256": plan["plan_sha256"],
        "write_status": write_status,
    }


def _state_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    plan = _read_json_object(args.plan, label="native plan")
    prior = (
        _read_json_object(args.prior_state, label="native prior state")
        if args.prior_state
        else None
    )
    if prior is not None:
        completed = prior.get("completed")
        if not isinstance(completed, list):
            raise NativeCliError("native prior state completed must be an array")
        expected_prior = reconcile_native_state(plan, completed)
        if prior != expected_prior:
            raise NativeCliError(
                "native prior state digest or derived completion state is invalid"
            )
    observations = [
        _read_json_object(path, label="native cell observation")
        for path in args.observation
    ]
    state = reconcile_native_state(plan, observations, prior_state=prior)
    output, write_status = _write_immutable_json(
        args.output, state, label="native state"
    )
    return EXIT_OK, {
        "command": "state",
        "completed_cells": len(state["completed"]),
        "output": str(output),
        "pending_cells": len(state["pending_cell_ids"]),
        "state_sha256": state["state_sha256"],
        "write_status": write_status,
    }


def _report_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if not args.results and not args.bundle:
        raise NativeCliError(
            "native report requires at least one --results JSONL or --bundle directory"
        )
    plan = _read_json_object(args.plan, label="native plan")
    inputs: list[Mapping[str, Any] | str] = []
    for path in args.results:
        inputs.extend(_read_results_jsonl(path))
    inputs.extend(args.bundle)
    report = build_native_report(plan, inputs)
    output, write_status = _write_immutable_json(
        args.output, report, label="native report"
    )
    incomplete = report["publication_status"] == (
        "incomplete_noncomparable_cells_excluded"
    )
    return (EXIT_INCOMPLETE if incomplete else EXIT_OK), {
        "command": "report",
        "complete_matched_blocks": report["coverage"]["complete_matched_blocks"],
        "output": str(output),
        "publication_status": report["publication_status"],
        "report_sha256": report["report_sha256"],
        "write_status": write_status,
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    run_native: Callable[[str], Any],
    run_error: type[BaseException],
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        try:
            outcome = run_native(args.config)
        except (run_error, ValueError, OSError) as exc:
            parser.exit(EXIT_ERROR, f"ERROR {exc}\n")
        print(
            json.dumps(
                {
                    "bundle": str(outcome.bundle_dir),
                    "results": str(outcome.results_path),
                    "run_id": outcome.row["run_id"],
                },
                sort_keys=True,
            )
        )
        return EXIT_OK

    try:
        if args.command == "plan":
            code, summary = _plan_command(args)
        elif args.command == "state":
            code, summary = _state_command(args)
        else:
            code, summary = _report_command(args)
    except NativeReportError as exc:
        parser.exit(EXIT_NONCOMPARABLE, f"NONCOMPARABLE {exc}\n")
    except (NativeCliError, NativeMatrixError, OSError, ValueError) as exc:
        parser.exit(EXIT_ERROR, f"ERROR {exc}\n")
    print(json.dumps(summary, sort_keys=True))
    return code


__all__ = [
    "EXIT_ERROR",
    "EXIT_INCOMPLETE",
    "EXIT_NONCOMPARABLE",
    "EXIT_OK",
    "NativeCliError",
    "main",
]
