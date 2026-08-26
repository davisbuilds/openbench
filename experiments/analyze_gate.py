#!/usr/bin/env python3
"""Timing-contamination gate analysis.

Compares glm-5.3-flash's per-task wall time SOLO (results/gate/solo) vs under
3-way concurrency (results/gate/concurrent). If concurrency systematically
inflates wall time beyond API-latency noise, arm-level --workers corrupts the
latency numbers and must not be used for scored latency comparisons.
"""
import json
import os
import statistics as st

ROOT = os.path.join(os.path.dirname(__file__), "..", "results", "gate")


def load(path, model="glm-5.3-flash"):
    rows = {}
    if not os.path.isfile(path):
        return rows
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("model") != model:
            continue
        # keep only successful, completed cells -- a failed/throttled cell's wall
        # time is not a clean latency sample
        if not r.get("success"):
            continue
        rows.setdefault(r["task"], []).append(r.get("wall_time_s", 0.0))
    return rows


def med(xs):
    return st.median(xs) if xs else float("nan")


def main():
    solo = load(os.path.join(ROOT, "solo", "results.jsonl"))
    conc = load(os.path.join(ROOT, "concurrent", "results.jsonl"))
    tasks = sorted(set(solo) | set(conc))
    print(f"{'task':<20} {'solo_med':>9} {'conc_med':>9} {'delta%':>8} {'n_solo':>6} {'n_conc':>6}")
    print("-" * 64)
    deltas = []
    for t in tasks:
        s, c = med(solo.get(t, [])), med(conc.get(t, []))
        if s and c and s == s and c == c:  # both present, non-nan
            d = (c - s) / s * 100
            deltas.append(d)
            flag = "  <-- inflated" if d > 20 else ""
            print(f"{t:<20} {s:>9.1f} {c:>9.1f} {d:>7.1f}% "
                  f"{len(solo.get(t,[])):>6} {len(conc.get(t,[])):>6}{flag}")
        else:
            print(f"{t:<20} {s:>9.1f} {c:>9.1f} {'--':>8} "
                  f"{len(solo.get(t,[])):>6} {len(conc.get(t,[])):>6}")
    print("-" * 64)
    if deltas:
        agg = st.median(deltas)
        print(f"median per-task wall-time delta: {agg:+.1f}%")
        n_infl = sum(1 for d in deltas if d > 20)
        print(f"tasks inflated >20%: {n_infl}/{len(deltas)}")
        # Gate verdict: pass if the median delta is within noise (<=15%) and the
        # inflation is not systematic (not a majority of tasks up >20%).
        systematic = n_infl > len(deltas) / 2
        if agg <= 15 and not systematic:
            print("\nGATE: PASS -- concurrency did not systematically inflate wall "
                  "time; --workers is safe for scored latency runs on distinct-"
                  "provider arms.")
        elif agg <= 15:
            print("\nGATE: MARGINAL -- median within noise but some tasks inflated; "
                  "inspect before trusting for latency-sensitive comparisons.")
        else:
            print("\nGATE: FAIL -- concurrency inflates wall time; use --workers for "
                  "THROUGHPUT only, not for scored latency comparisons.")
    else:
        print("no comparable (task present in both, successful) cells yet")


if __name__ == "__main__":
    main()
