#!/usr/bin/env python3
"""Score engine.py against the formula grids.

Invoked by checker.sh with cwd = the agent's workspace copy. Imports
``engine.evaluate`` from that cwd and runs it on each grid in cases.json.
Numbers are compared with a small tolerance; error markers (``#CYCLE``,
``#DIV/0``) are compared exactly. SCORE is the fraction of individual cells
computed correctly across every grid; exit 0 only when all cells match.
"""
import importlib.util
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_engine():
    path = os.path.join(os.getcwd(), "engine.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("agent_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cell_ok(want, got):
    if isinstance(want, str):
        return got == want
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        return False
    return math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9)


def main():
    with open(os.path.join(HERE, "cases.json"), encoding="utf-8") as fh:
        cases = json.load(fh)
    engine = load_engine()

    total = 0
    correct = 0
    all_ok = True
    for case in cases:
        expected = case["expected"]
        total += len(expected)
        result = {}
        if engine is not None:
            try:
                result = engine.evaluate(dict(case["cells"]))
            except Exception as exc:  # noqa: BLE001 - a crash fails the whole grid
                print("FAIL {}: {}".format(case["name"], exc))
                all_ok = False
        case_ok = True
        for name, want in expected.items():
            if isinstance(result, dict) and cell_ok(want, result.get(name)):
                correct += 1
            else:
                case_ok = False
        if not case_ok:
            all_ok = False
            print("FAIL {}".format(case["name"]))

    print("{}/{} cells correct".format(correct, total))
    print("SCORE: {:.4f}".format((correct / total) if total else 0.0))
    return 0 if (total and all_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
