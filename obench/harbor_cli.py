"""Command-line interface for OpenBench-managed Harbor execution."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any

from .export_harbor import ExportError, export_tasks
from .harbor_job import HarborJobError
from .harbor_oauth import HarborOAuthError
from .harbor_profiles import HarborProfileError
from .harbor_run import (
    HarborRunError,
    run_harbor_oauth,
    run_harbor_profile_job,
)


class _ExitRecordingProcessRunner:
    """Run subprocesses while retaining only the Harbor trial's exit code."""

    def __init__(self) -> None:
        self.harbor_returncode: int | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        completed = subprocess.run(argv, **kwargs)
        if len(argv) > 1 and argv[1] == "run":
            self.harbor_returncode = int(completed.returncode)
        return completed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obench harbor",
        description="Run one exported OpenBench task through Harbor.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    oauth_run = subparsers.add_parser(
        "oauth-run",
        help="run one trial with a host-managed Codex OAuth auth.json",
        description=(
            "Run one exported task with Codex OAuth. Credential contents are "
            "staged through private files, never command arguments or environment."
        ),
    )
    oauth_run.add_argument(
        "--task",
        required=True,
        metavar="EXPORTED_TASK",
        help="exported Harbor 1.4 task directory",
    )
    oauth_run.add_argument(
        "--model",
        required=True,
        help="explicit Harbor model identifier",
    )
    oauth_run.add_argument(
        "--master-auth-json",
        required=True,
        metavar="PATH",
        help="host auth.json to stage and update after the trial",
    )
    oauth_run.add_argument(
        "--jobs-dir",
        required=True,
        metavar="DIR",
        help="Harbor jobs output directory",
    )
    oauth_run.add_argument(
        "--job-name",
        required=True,
        help="new Harbor job name",
    )
    oauth_run.add_argument(
        "--harbor-binary",
        default="harbor",
        metavar="PATH",
        help="Harbor executable name or path (default: harbor)",
    )
    job_run = subparsers.add_parser(
        "job-run",
        help="run a native task x harness x attempt matrix",
        description=(
            "Export OpenBench tasks and run a native Harbor job with exact "
            "stock auth profiles. Harbor owns scheduling, retries, locking, "
            "and resume."
        ),
    )
    job_run.add_argument(
        "--tasks-dir",
        required=True,
        metavar="DIR",
        help="OpenBench task root containing task directories",
    )
    job_run.add_argument(
        "--task",
        required=True,
        help="'all' or a comma-separated OpenBench task selection",
    )
    job_run.add_argument(
        "--export-dir",
        required=True,
        metavar="DIR",
        help="directory for deterministic Harbor task exports",
    )
    job_run.add_argument(
        "--harness",
        action="append",
        required=True,
        choices=("codex", "pi", "opencode", "cursor", "devin"),
        help="stock harness profile; repeat for multiple harnesses",
    )
    job_run.add_argument(
        "--model",
        required=True,
        choices=("gpt-5.5-medium", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
        help="OpenBench model profile shared across harnesses",
    )
    job_run.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="measured attempts per task/harness arm (default: 1)",
    )
    job_run.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="maximum concurrent Harbor trials (default: 1)",
    )
    job_run.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="Harbor infrastructure retries per trial (default: 0)",
    )
    job_run.add_argument(
        "--jobs-dir",
        required=True,
        metavar="DIR",
        help="Harbor jobs output directory",
    )
    job_run.add_argument(
        "--job-name",
        required=True,
        help="Harbor job name; an identical existing job is resumed",
    )
    job_run.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="immutable generated Harbor job config JSON",
    )
    job_run.add_argument(
        "--harbor-binary",
        default="harbor",
        metavar="PATH",
        help="Harbor executable name or path (default: harbor)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    process_runner = _ExitRecordingProcessRunner()
    if args.command == "job-run":
        return _run_profile_job(args, process_runner)
    if args.command != "oauth-run":
        return 2

    try:
        result = run_harbor_oauth(
            task_dir=args.task,
            model=args.model,
            master_auth_json=args.master_auth_json,
            jobs_dir=args.jobs_dir,
            job_name=args.job_name,
            harbor_binary=args.harbor_binary,
            run_process=process_runner,
        )
    except (HarborRunError, HarborOAuthError, OSError, ValueError) as exc:
        if process_runner.harbor_returncode is not None:
            returncode = process_runner.harbor_returncode
            print(
                f"ERROR: Harbor exited with code {returncode}, but OAuth "
                f"credential finalization failed ({type(exc).__name__})",
                file=sys.stderr,
            )
            return returncode if returncode != 0 else 2
        if isinstance(exc, (HarborRunError, HarborOAuthError)):
            detail = f": {exc}"
        else:
            detail = f" ({type(exc).__name__})"
        print(f"ERROR: Harbor OAuth run could not start{detail}", file=sys.stderr)
        return 2

    message = (
        f"Harbor exited with code {result.returncode}; "
        f"expected job output: {result.expected_job_path}"
    )
    print(message, file=sys.stdout if result.returncode == 0 else sys.stderr)
    return result.returncode


def _run_profile_job(
    args: argparse.Namespace,
    process_runner: _ExitRecordingProcessRunner,
) -> int:
    try:
        exports = export_tasks(
            args.tasks_dir,
            args.export_dir,
            args.task,
            network_mode="public",
        )
        task_names = tuple(
            os.path.basename(os.path.normpath(item["out_dir"]))
            for item in exports
        )
        result = run_harbor_profile_job(
            exported_tasks_dir=args.export_dir,
            task_names=task_names,
            harnesses=tuple(args.harness),
            model=args.model,
            attempts=args.attempts,
            n_concurrent_trials=args.concurrency,
            max_retries=args.max_retries,
            jobs_dir=args.jobs_dir,
            job_name=args.job_name,
            config_path=args.config,
            harbor_binary=args.harbor_binary,
            run_process=process_runner,
        )
    except (
        ExportError,
        HarborJobError,
        HarborOAuthError,
        HarborProfileError,
        HarborRunError,
    ) as exc:
        if process_runner.harbor_returncode is not None:
            returncode = process_runner.harbor_returncode
            print(
                f"ERROR: Harbor exited with code {returncode}, but OAuth "
                f"credential finalization failed ({type(exc).__name__})",
                file=sys.stderr,
            )
            return returncode if returncode != 0 else 2
        print(f"ERROR: Harbor job could not start: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        if process_runner.harbor_returncode is not None:
            returncode = process_runner.harbor_returncode
            print(
                f"ERROR: Harbor exited with code {returncode}, but job "
                f"finalization failed ({type(exc).__name__})",
                file=sys.stderr,
            )
            return returncode if returncode != 0 else 2
        print(
            f"ERROR: Harbor job could not start ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    action = "resumed" if result.resumes_existing_job else "started"
    message = (
        f"Harbor job {action}; exited with code {result.returncode}; "
        f"trials: {result.artifact.trial_count}; "
        f"config sha256: {result.artifact.sha256}; "
        f"job output: {result.expected_job_path}"
    )
    print(message, file=sys.stdout if result.returncode == 0 else sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
