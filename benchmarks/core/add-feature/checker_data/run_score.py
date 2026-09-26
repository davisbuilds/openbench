#!/usr/bin/env python3
"""Score the add-feature task.

SCORE = (passing feature tests + fully-green regression groups) / (n_feature +
n_regression_groups).

Invoked by checker.sh with cwd = the agent's workspace copy. The library under
test (``miniconf``) is imported from that cwd. The feature tests and the
regression suite are pristine copies shipped here, so the score cannot be gamed
by editing the workspace tests. Feature tests are scored individually; each
regression module is scored all-or-nothing as one "group" so an agent gets no
credit for a change that breaks existing behavior.

Prints per-item FAIL lines, a summary, and a final ``SCORE: <0.0-1.0>`` line.
Exit 0 only if every feature test passes and every regression group is green.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FEATURE_DIR = os.path.join(HERE, "feature")
REGRESSION_DIR = os.path.join(HERE, "regression")


def _read_list(path):
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip()]


def _run_one(loader, name):
    """Load and run a single test id or module; return (ran_any, success)."""
    try:
        suite = loader.loadTestsFromName(name)
    except Exception:
        return False, False
    result = unittest.TestResult()
    suite.run(result)
    return result.testsRun > 0, result.wasSuccessful()


def main():
    # Test dirs first so bare module names resolve to the pristine copies; the
    # workspace cwd supplies ``miniconf``.
    sys.path.insert(0, os.getcwd())
    sys.path.insert(0, REGRESSION_DIR)
    sys.path.insert(0, FEATURE_DIR)

    feature_ids = _read_list(os.path.join(FEATURE_DIR, "feature_ids.txt"))
    reg_modules = _read_list(os.path.join(REGRESSION_DIR, "reg_modules.txt"))

    loader = unittest.TestLoader()

    feat_pass = 0
    for test_id in feature_ids:
        ran, ok = _run_one(loader, test_id)
        if ran and ok:
            feat_pass += 1
        else:
            print("FAIL feature {}".format(test_id))

    reg_green = 0
    for module in reg_modules:
        ran, ok = _run_one(loader, module)
        if ran and ok:
            reg_green += 1
        else:
            print("FAIL regression-group {}".format(module))

    denom = len(feature_ids) + len(reg_modules)
    passed = feat_pass + reg_green
    score = (passed / denom) if denom else 0.0
    print("feature {}/{}, regression-groups {}/{}".format(
        feat_pass, len(feature_ids), reg_green, len(reg_modules)))
    print("SCORE: {:.4f}".format(score))
    return 0 if passed == denom else 1


if __name__ == "__main__":
    raise SystemExit(main())
