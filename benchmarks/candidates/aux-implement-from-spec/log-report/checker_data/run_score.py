#!/usr/bin/env python3
"""Score report.py against a set of command invocations.

Invoked by checker.sh with cwd = the agent's workspace copy. For each case in
cases.json we run ``python3 report.py <access.log> <args...>`` and compare its
stdout, line by line, to the expected output. SCORE is the fraction of expected
lines reproduced exactly (aligned by position); exit 0 only when every command
matches in full.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGFILE = os.path.join(HERE, "access.log")


def main():
    with open(os.path.join(HERE, "cases.json"), encoding="utf-8") as fh:
        cases = json.load(fh)

    report = os.path.join(os.getcwd(), "report.py")
    if not os.path.isfile(report):
        print("FAIL: report.py not found in workspace")
        print("SCORE: 0.0")
        return 1

    total = 0
    matched = 0
    all_exact = True
    for case in cases:
        expected = case["expected"].splitlines()
        total += len(expected)
        try:
            proc = subprocess.run(
                [sys.executable, report, LOGFILE] + case["args"],
                capture_output=True, text=True, timeout=30)
            got = proc.stdout.splitlines()
        except Exception as exc:  # noqa: BLE001 - any crash is a failed command
            got = []
            print("FAIL {}: {}".format(case["name"], exc))
            all_exact = False
        for i, want in enumerate(expected):
            if i < len(got) and got[i] == want:
                matched += 1
        if got != expected:
            all_exact = False
            print("FAIL {} ({})".format(case["name"], " ".join(case["args"])))

    print("{}/{} expected lines correct".format(matched, total))
    print("SCORE: {:.4f}".format((matched / total) if total else 0.0))
    return 0 if all_exact else 1


if __name__ == "__main__":
    raise SystemExit(main())
