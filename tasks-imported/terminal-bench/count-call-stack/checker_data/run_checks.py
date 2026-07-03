#!/usr/bin/env python3
"""Stdlib port of Terminal-Bench 'count-call-stack' tests/test_outputs.py.

cwd = a fresh copy of the workspace. Compares ./output.txt against the upstream
expected_output.txt (shipped in checker_data/) line-by-line with trailing
whitespace stripped, matching the upstream pytest assertion exactly.
"""
import os
import sys

EXPECTED = os.path.join(os.environ["TASK_DIR"], "checker_data", "expected_output.txt")


def main():
    if not os.path.exists("output.txt"):
        print("FAIL: output.txt does not exist")
        sys.exit(1)
    with open("output.txt", "r", encoding="utf-8", errors="replace") as f:
        content = f.read().strip()
    with open(EXPECTED, "r", encoding="utf-8") as f:
        expected = f.read().strip()
    content_lines = [ln.rstrip() for ln in content.splitlines()]
    expected_lines = [ln.rstrip() for ln in expected.splitlines()]
    if content_lines != expected_lines:
        # Report the first differing line for debuggability.
        for i, (a, b) in enumerate(zip(content_lines, expected_lines)):
            if a != b:
                print(f"FAIL: output.txt differs at line {i}")
                print(f"  got:      {a!r}")
                print(f"  expected: {b!r}")
                sys.exit(1)
        print(f"FAIL: output.txt line count differs "
              f"(got {len(content_lines)}, expected {len(expected_lines)})")
        sys.exit(1)
    print("PASS: output matches expected")
    sys.exit(0)


if __name__ == "__main__":
    main()
