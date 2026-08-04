#!/usr/bin/env python3
"""Umbrella CLI for the OpenBench harness benchmarking framework.

    obench run | report | doctor | validate | admit | gateway | router | harbor | gate |
         compare | init | publish | verify | community | leaderboard | site | pack |
         export | import
         [args...]
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

    sub.add_parser("run", help="run a Harbor-native benchmark suite", add_help=False)
    sub.add_parser(
        "legacy",
        help="explicit compatibility commands for the old native runner",
        add_help=False,
    )
    sub.add_parser("report", help="aggregate results.jsonl", add_help=False)
    sub.add_parser("doctor", help="preflight CLI/auth/model checks", add_help=False)
    sub.add_parser("validate", help="check task checker polarity", add_help=False)
    sub.add_parser("admit", help="run the full task admission gate", add_help=False)
    sub.add_parser(
        "gateway",
        help="compare fixed model/provider routes through AI gateways",
        add_help=False,
    )
    sub.add_parser(
        "router",
        help="inspect and benchmark native model-router behavior",
        add_help=False,
    )
    sub.add_parser(
        "harbor",
        help="run exported tasks through Harbor",
        add_help=False,
    )
    sub.add_parser("gate", help="BYO candidate admission gate", add_help=False)
    sub.add_parser("compare", help="matched-denominator scorecard", add_help=False)
    sub.add_parser("init", help="scaffold .openbench/ for private evals", add_help=False)
    sub.add_parser("publish", help="build a shareable comparison bundle", add_help=False)
    sub.add_parser("verify", help="re-verify a publish bundle's digests", add_help=False)
    sub.add_parser(
        "community",
        help="manage community publish-bundle submissions",
        add_help=False,
    )
    sub.add_parser(
        "leaderboard",
        help="alias for `site` (kept for existing scripts)",
        add_help=False,
    )
    sub.add_parser(
        "site",
        help="build the harness+gateway leaderboard site (docs/index.html)",
        add_help=False,
    )
    sub.add_parser(
        "pack",
        help="install and manage versioned packs (tasks or harness manifests)",
        add_help=False,
    )
    sub.add_parser("matrix", help="retry-aware queue-based benchmark runner", add_help=False)
    sub.add_parser("results", help="query results: summary/pertask/matched/errors/evidence", add_help=False)
    sub.add_parser("export", help="export tasks to external formats (harbor)", add_help=False)
    sub.add_parser("import", help="import tasks from external formats (harbor)", add_help=False)

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

    known = {
        "run", "legacy", "report", "doctor", "validate", "admit", "gateway", "router", "harbor", "gate", "compare", "init",
        "matrix", "results", "publish", "verify", "community", "leaderboard",
        "site", "pack", "export",
        "import",
    }
    if command not in known:
        parser.error(
            f"unknown command {command!r}; choose from run, legacy, report, doctor, "
            "validate, admit, gateway, router, harbor, gate, compare, init, publish, verify, community, "
            "leaderboard, results, site, pack, export, import"
        )

    if command == "results":
        from .results_query import main as results_main
        return results_main(rest)
    if command == "matrix":
        from .matrix_queue import main as matrix_main
        return matrix_main(rest)
    if command == "run":
        from .suite_run import main as run_main
        return run_main(rest)
    if command == "legacy":
        if not rest or rest[0] != "run":
            parser.error("legacy requires the explicit subcommand: obench legacy run")
        from .run import main as legacy_run_main
        return legacy_run_main(rest[1:])
    if command == "report":
        from .report import main as report_main
        return report_main(rest)
    if command == "doctor":
        from .doctor import main as doctor_main
        return doctor_main(rest)
    if command == "validate":
        from .validate_tasks import main as validate_main
        return validate_main(rest)
    if command == "admit":
        from .admission_gate import main as admission_main
        return admission_main(rest)
    if command == "gateway":
        from .gateway_cli import main as gateway_main
        return gateway_main(rest)
    if command == "router":
        from .router_cli import main as router_main
        return router_main(rest)
    if command == "harbor":
        from .harbor_cli import main as harbor_main
        return harbor_main(rest)
    if command == "gate":
        from .candidate_gate import main as gate_main
        return gate_main(rest)
    if command == "compare":
        from .compare import main as compare_main
        return compare_main(rest)
    if command == "init":
        from .init import main as init_main
        return init_main(rest)
    if command == "publish":
        from .publish import _publish_main
        return _publish_main(rest)
    if command == "verify":
        from .publish import _verify_main
        return _verify_main(rest)
    if command == "community":
        from .community import main as community_main
        return community_main(rest)
    if command == "leaderboard":
        from .leaderboard import main as leaderboard_main
        return leaderboard_main(rest)
    if command == "site":
        from .site import main as site_main
        return site_main(rest)
    if command == "pack":
        from .packs import main as pack_main
        return pack_main(rest)
    if command == "export":
        from .export_harbor import main as export_main
        return export_main(rest)
    if command == "import":
        if rest and rest[0] == "harbor-results":
            from .harbor_results import main as harbor_results_main
            return harbor_results_main(rest[1:])
        from .import_harbor import main as import_main
        return import_main(rest)
    return 1


def _version_string():
    from . import __version__
    return f"obench {__version__}"


if __name__ == "__main__":
    raise SystemExit(main())
