#!/usr/bin/env python3
"""Grade the performance-budget task.

    SCORE = GATE * (perf_tiers_passed / perf_tiers_total)

where GATE is 1.0 only if EVERY correctness case reproduces the oracle answer
exactly, else 0.0 (a hard, all-or-nothing gate like webcore's regression gate).
A perf tier passes only when the agent's output matches the oracle AND the
measured CPU time is within that tier's budget -- correctness is verified on the
SAME run that is timed, so a solver cannot trade correctness for speed.

Design intent (calibrated on the authoring machine, see calibration.md):
  * the shipped naive O(n^2) solver is correct -> clears the GATE, and clears
    the three small perf tiers but not the three large ones -> ~0.50.
  * an O(n log n) solver clears every tier -> 1.00.

Anti-cheat:
  * Inputs are freshly generated per run from a random base seed, so nothing
    can be precomputed or size-special-cased to a fixed answer.
  * Ground truth comes from an INDEPENDENT merge-sort oracle, self-checked
    against a brute-force pass on small inputs on every run.
  * Each case runs in a subprocess with a wall-clock kill, so a runaway solver
    (or the naive one on a huge tier) cannot block or game grading.

Residual machine-dependence (wall/CPU budgets are inherently hardware-relative)
is documented in the task README and calibration.md; budgets carry large
headroom (reference is 15x-1000x under budget) to keep the 1.0 result robust.
"""
import importlib.util
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMMON_PATH = os.path.join(HERE, "common.py")
RUN_TIER = os.path.join(HERE, "run_tier.py")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


common = load(COMMON_PATH, "_checker_common")
oracle = load(os.path.join(HERE, "oracle.py"), "_checker_oracle")

# --- Correctness gate: (name, n, mode). Small, so the naive solver clears it
#     instantly; a wrong solver fails at least one and forfeits everything. ---
GATE_CASES = [
    ("empty", 0, "uniform"),
    ("single", 1, "uniform"),
    ("two_rev", 2, "reverse"),
    ("const_200", 200, "constant"),
    ("sorted_500", 500, "sorted"),
    ("reverse_500", 500, "reverse"),
    ("fewvals_300", 300, "fewvals"),
    ("negatives_800", 800, "negatives"),
    ("wide_1000", 1000, "wide"),
    ("uniform_1000", 1000, "uniform"),
    ("uniform_2000", 2000, "uniform"),
    ("fewvals_2000", 2000, "fewvals"),
]

# --- Size-tiered performance budgets: (name, n, mode, budget_cpu_seconds).
#     Calibrated so naive passes the first three and not the last three. ---
PERF_TIERS = [
    ("perf_2k", 2000, "uniform", 1.0),
    ("perf_8k", 8000, "uniform", 3.0),
    ("perf_12k", 12000, "uniform", 6.0),
    ("perf_100k", 100000, "uniform", 8.0),
    ("perf_350k", 350000, "uniform", 12.0),
    ("perf_1m", 1000000, "uniform", 20.0),
]


def run_case(workspace, seed, n, mode, kill_cap):
    """Run one case in a subprocess. Returns (ok_run, cpu_time, out_digest).

    ok_run is False on timeout, crash, or missing output.
    """
    try:
        proc = subprocess.run(
            [sys.executable, RUN_TIER, workspace, COMMON_PATH,
             str(seed), str(n), mode],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=kill_cap,
        )
    except subprocess.TimeoutExpired:
        return False, None, None
    if proc.returncode != 0:
        return False, None, None
    cpu = None
    dig = None
    for line in proc.stdout.splitlines():
        if line.startswith("TIME: "):
            cpu = float(line[len("TIME: "):])
        elif line.startswith("DIGEST: "):
            dig = line[len("DIGEST: "):].strip()
    if cpu is None or dig is None:
        return False, None, None
    return True, cpu, dig


def main():
    workspace = os.getcwd()
    base_seed = random.SystemRandom().randrange(2**60)

    # Self-check: the independent oracle must agree with brute force on a few
    # tiny inputs, every run -- guards against an oracle regression.
    for s in range(6):
        v = common.generate(base_seed + 9000 + s, 40, "uniform")
        if oracle.count_smaller_after(v) != oracle.brute_force(v):
            print("ORACLE SELF-CHECK FAILED")
            print("SCORE: 0.0")
            return 1

    # --- Correctness gate ---
    gate_ok = True
    for i, (name, n, mode) in enumerate(GATE_CASES):
        seed = base_seed + i
        expected = common.digest(oracle.count_smaller_after(
            common.generate(seed, n, mode)))
        # Generous kill cap: correctness only, not a timing check.
        ok, _cpu, dig = run_case(workspace, seed, n, mode, kill_cap=60)
        passed = ok and dig == expected
        if not passed:
            gate_ok = False
            print("GATE FAIL: %s (n=%d, mode=%s)" % (name, n, mode))
    print("gate: %s (%d cases)" % ("PASS" if gate_ok else "FAIL", len(GATE_CASES)))

    # --- Performance tiers ---
    perf_passed = 0
    for i, (name, n, mode, budget) in enumerate(PERF_TIERS):
        seed = base_seed + 1000 + i
        expected = common.digest(oracle.count_smaller_after(
            common.generate(seed, n, mode)))
        kill_cap = budget * 2 + 5
        ok, cpu, dig = run_case(workspace, seed, n, mode, kill_cap)
        correct = ok and dig == expected
        in_budget = ok and cpu is not None and cpu <= budget
        tier_ok = correct and in_budget
        if tier_ok:
            perf_passed += 1
        cpu_s = "%.3f" % cpu if cpu is not None else "----"
        status = "PASS" if tier_ok else "FAIL"
        reason = ""
        if not ok:
            reason = " [killed/crash > %.1fs wall]" % kill_cap
        elif not correct:
            reason = " [wrong output]"
        elif not in_budget:
            reason = " [over budget]"
        print("%-9s n=%-8d cpu=%-7s budget=%4.1fs  %s%s"
              % (name, n, cpu_s, budget, status, reason))

    perf_total = len(PERF_TIERS)
    score = (1.0 if gate_ok else 0.0) * (perf_passed / perf_total)
    print("perf: %d/%d tiers within budget" % (perf_passed, perf_total))
    print("SCORE: %.4f" % score)
    return 0 if (gate_ok and perf_passed == perf_total) else 1


if __name__ == "__main__":
    raise SystemExit(main())
