#!/usr/bin/env python3
"""Generic scorer for imported Exercism tasks.

Runs the agent's ``solution.py`` (found in the current working directory, which
the runner sets to a fresh copy of the task workspace) against the flattened
canonical cases in ``cases.json`` shipped next to this script. Each case is run
in isolation so one broken function only fails its own cases; the denominator
stays fixed. Prints per-failure lines, a summary, and a final ``SCORE:`` line;
exits 0 only when every case passes.

Case shapes in cases.json:
  {"fn": "<name>", "kwargs": {...}, "expected": <value>}   fn(**kwargs) == expected
  {"fn": "<name>", "kwargs": {...}, "error": true}         fn(**kwargs) must raise ValueError
  {"roundtrip": ["<outer>", "<inner>"], "kwargs": {...}, "expected": <value>}
                                                           outer(inner(**kwargs)) == expected
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_solution():
    path = os.path.join(os.getcwd(), "solution.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("agent_solution", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_case(mod, case):
    if "roundtrip" in case:
        outer, inner = case["roundtrip"]
        return getattr(mod, outer)(getattr(mod, inner)(**case["kwargs"])) == case["expected"]
    fn = getattr(mod, case["fn"], None)
    if not callable(fn):
        return False
    try:
        got = fn(**case["kwargs"])
    except ValueError:
        return bool(case.get("error"))
    if case.get("error"):
        return False
    return got == case["expected"]


def main():
    with open(os.path.join(HERE, "cases.json"), encoding="utf-8") as fh:
        cases = json.load(fh)
    mod = load_solution()

    total = len(cases)
    passed = 0
    failures = []
    for i, case in enumerate(cases):
        ok = False
        if mod is not None:
            try:
                ok = run_case(mod, case)
            except Exception:
                ok = False
        if ok:
            passed += 1
        else:
            failures.append(case.get("desc", "case {}".format(i)))

    for desc in failures[:50]:
        print("FAIL {}".format(desc))
    print("{}/{} cases passing".format(passed, total))
    print("SCORE: {:.4f}".format((passed / total) if total else 0.0))
    return 0 if (total and passed == total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
