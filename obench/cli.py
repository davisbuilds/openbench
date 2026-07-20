#!/usr/bin/env python3
"""Umbrella CLI for the OpenBench harness benchmarking framework.

    obench run | report | doctor | validate | gate | compare [args...]
"""

from __future__ import annotations

import argparse
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="obench",
        description="OpenBench harness benchmarking (install name: obench).",
    )
    parser.add_argument(
        "--version", action="version",
        version=_version_string(),
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="run benchmark cells", add_help=False)
    sub.add_parser("report", help="aggregate results.jsonl", add_help=False)
    sub.add_parser("doctor", help="preflight CLI/auth/model checks", add_help=False)
    sub.add_parser("validate", help="check task checker polarity", add_help=False)
    sub.add_parser("gate", help="BYO candidate admission gate", add_help=False)
    sub.add_parser("compare", help="matched-denominator scorecard", add_help=False)

    if not argv:
        parser.print_help()
        return 0

    command = argv[0]
    rest = argv[1:]

    if command in ("-h", "--help"):
        parser.print_help()
        return 0
    if command in ("-V", "--version"):
        parser.parse_args([command])
        return 0

    if command not in {"run", "report", "doctor", "validate", "gate", "compare"}:
        parser.error(
            f"unknown command {command!r}; choose from run, report, doctor, "
            "validate, gate, compare"
        )

    if command == "run":
        from .run import main as run_main
        return run_main(rest)
    if command == "report":
        from .report import main as report_main
        return report_main(rest)
    if command == "doctor":
        from .doctor import main as doctor_main
        return doctor_main(rest)
    if command == "validate":
        from .validate_tasks import main as validate_main
        return validate_main(rest)
    if command == "gate":
        from .candidate_gate import main as gate_main
        return gate_main(rest)
    if command == "compare":
        from .compare import main as compare_main
        return compare_main(rest)
    return 1


def _version_string():
    from . import __version__
    return f"obench {__version__}"


if __name__ == "__main__":
    raise SystemExit(main())
