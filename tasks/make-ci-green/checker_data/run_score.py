#!/usr/bin/env python3
"""Score the catalog test suite: SCORE = fraction of tests passing.

Invoked by checker.sh with cwd = the agent's workspace copy. The library under
test (``catalog``) is imported from that cwd, but the tests themselves are run
from a pristine copy shipped alongside this script, so the score cannot be
gamed by editing the workspace tests.

Each test id is loaded and run in isolation. That keeps the denominator fixed
(``len(test_ids)``): a broken import in one library module only fails the tests
that touch that module rather than aborting collection for the whole suite.

Prints one ``FAIL <id>`` line per failing test, a summary, and a final
``SCORE: <0.0-1.0>`` line. Exit 0 only if every test passes.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE_DIR = os.path.join(HERE, "suite")


def main():
    # Pristine suite modules first (test_*.py), then the workspace cwd so
    # ``import catalog`` resolves to the agent's code under test.
    sys.path.insert(0, os.getcwd())
    sys.path.insert(0, SUITE_DIR)

    with open(os.path.join(SUITE_DIR, "test_ids.txt"), encoding="utf-8") as fh:
        test_ids = [ln.strip() for ln in fh if ln.strip()]

    total = len(test_ids)
    passed = 0
    failures = []
    loader = unittest.TestLoader()
    for test_id in test_ids:
        try:
            suite = loader.loadTestsFromName(test_id)
        except Exception:
            failures.append(test_id)
            continue
        result = unittest.TestResult()
        suite.run(result)
        if result.testsRun == 1 and result.wasSuccessful():
            passed += 1
        else:
            failures.append(test_id)

    score = (passed / total) if total else 0.0
    for test_id in failures:
        print("FAIL {}".format(test_id))
    print("{}/{} tests passing".format(passed, total))
    print("SCORE: {:.4f}".format(score))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
