"""Command-line interface for OpenBench-managed Harbor execution."""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Any

from .harbor_oauth import HarborOAuthError
from .harbor_run import HarborRunError, run_harbor_oauth


class _ExitRecordingProcessRunner:
    """Run subprocesses while retaining only the Harbor trial's exit code."""

    def __init__(self) -> None:
        self.harbor_returncode: int | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        completed = subprocess.run(argv, **kwargs)
        if not argv or argv[-1] != "--version":
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "oauth-run":
        return 2

    process_runner = _ExitRecordingProcessRunner()
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


if __name__ == "__main__":
    raise SystemExit(main())
