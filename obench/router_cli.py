"""Router Bench command group.

The evidence probe is deliberately smaller than Router Bench. It proves which
route facts a native router exposes before those facts become benchmark fields.
"""

from __future__ import annotations

import argparse

from . import router_evidence_probe


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obench router",
        description="Inspect native model-router evidence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser(
        "evidence-probe",
        help="run privacy-safe live route-evidence probes",
    )
    probe.add_argument(
        "--router",
        action="append",
        choices=sorted(router_evidence_probe.SUPPORTED_ROUTERS),
        dest="routers",
        help="router to probe; repeatable (default: every supported router)",
    )
    probe.add_argument(
        "--case",
        action="append",
        choices=sorted(router_evidence_probe.PROBE_CASES),
        dest="cases",
        help="probe case; repeatable (default: every case)",
    )
    probe.add_argument(
        "--output",
        required=True,
        help="write the sanitized evidence artifact to this JSON file",
    )
    probe.add_argument(
        "--max-output-tokens",
        type=int,
        default=64,
        help="maximum generated tokens per request (default: 64)",
    )
    probe.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="repeat every router/case pair (default: 1)",
    )
    probe.add_argument(
        "--trace-timeout",
        type=float,
        default=15.0,
        help="seconds to wait for post-request trace reconciliation",
    )

    verify = sub.add_parser(
        "evidence-verify",
        help="verify a saved evidence artifact and its digest",
    )
    verify.add_argument("artifact")

    args = parser.parse_args(argv)
    if args.command == "evidence-probe":
        return router_evidence_probe.probe_main(args)
    if args.command == "evidence-verify":
        return router_evidence_probe.verify_main(args.artifact)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
