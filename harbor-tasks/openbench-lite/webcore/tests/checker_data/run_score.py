#!/usr/bin/env python3
"""Score webcore with an ALL-OR-NOTHING regression gate.

This is a deliberate variant of the canonical webcore run_score.py: it
keeps the same anti-cheat (each test id is loaded in ISOLATION from the pristine
checker_data/suite/, never from the agent's workspace) but changes only the
score AGGREGATION.

    SCORE = 0.3 * (1 if ALL regression tests pass else 0)
          + 0.7 * (fraction of feature tests passing)

Regression is all-or-nothing on purpose: the 0.3 block is awarded only when
*every* test_regression_* test passes, and a SINGLE regression failure forfeits
the entire block (there is no partial regression credit). This is the
anti-gaming choice for a feature-add task -- it refuses to reward feature work
that breaks existing behavior, so the only way to climb toward 1.0 is to add the
feature WITHOUT regressing. Feature tests (test_feature_*) are scored
individually. Exit 0 only when every test (regression and feature) passes."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = os.path.join(HERE, "suite")
REG_W, FEAT_W = 0.3, 0.7


def main():
    # Workspace cwd first so ``import webcore`` resolves to the agent's code;
    # the pristine suite dir is added after so the tests load from here, not
    # from anything the agent may have dropped in the workspace.
    sys.path.insert(0, os.getcwd())
    sys.path.insert(0, SUITE)

    with open(os.path.join(SUITE, "test_ids.txt"), encoding="utf-8") as fh:
        ids = [ln.strip() for ln in fh if ln.strip()]

    loader = unittest.TestLoader()

    def run(tid):
        try:
            suite = loader.loadTestsFromName(tid)
        except Exception:
            return False
        result = unittest.TestResult()
        suite.run(result)
        return result.testsRun >= 1 and result.wasSuccessful()

    results = {tid: run(tid) for tid in ids}
    reg = [t for t in ids if t.startswith("test_regression")]
    feat = [t for t in ids if t.startswith("test_feature")]
    reg_pass = sum(results[t] for t in reg)
    feat_pass = sum(results[t] for t in feat)
    reg_all = len(reg) > 0 and reg_pass == len(reg)
    score = REG_W * (1.0 if reg_all else 0.0) + FEAT_W * (
        feat_pass / len(feat) if feat else 0.0
    )

    for t in ids:
        if not results[t]:
            label = "FAIL(reg) " if t.startswith("test_regression") else "FAIL(feat) "
            print(label + t)
    print("regression %d/%d  feature %d/%d" % (reg_pass, len(reg), feat_pass, len(feat)))
    print("SCORE: %.4f" % score)
    return 0 if (reg_all and feat_pass == len(feat)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
