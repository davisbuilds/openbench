# perf-budget — a performance-budget task (maintainer notes)

**Not shown to the agent.** The harness copies only `workspace/` and shows
`instruction.md`. This file, `solution/`, and `checker_data/` stay hidden.

## What this task measures

This is the first **performance-budget** task (response (b) in RESULTS.md's
task-difficulty finding): the score is a *continuous performance margin*, not a
pass/fail an agent can saturate by merely being correct. Being correct is table
stakes — it only clears the gate and the small tiers (~0.5). Reaching 1.0
requires a genuine **complexity-class** improvement.

## The domain and why

Count-smaller-after-self (for each element, how many strictly-smaller elements
lie to its right). The gap is real and textbook-clean:

- **Naive** (shipped in `workspace/`): the obvious pair scan, **O(n^2)**.
- **Optimal** (`solution/`): coordinate-compress values to dense ranks, sweep
  right-to-left with a **Fenwick tree (BIT)**, prefix-query ranks below the
  current value — **O(n log n)**.

The optimal approach is nontrivial but well-defined (Fenwick/BIT, or an
equivalent merge-sort / order-statistic-tree formulation), and the improvement
is a complexity class, not micro-optimization: at n = 1,000,000 the naive solver
needs ~10^12 comparisons (hours) while the reference finishes in ~1s. There is
no constant-factor trick that closes an 11x gap at 100k and grows from there —
the only way up the tiers is a better algorithm.

## Scoring

```
SCORE = GATE * (perf_tiers_passed / perf_tiers_total)
```

- **GATE** (hard, all-or-nothing, like webcore's regression gate): 1.0 only if
  **all 12 correctness cases** reproduce the independent oracle's answer
  exactly; a single miss forfeits the whole score. This refuses to reward a fast
  wrong answer.
- **perf_tiers**: 6 size tiers (n = 2k, 8k, 12k, 100k, 350k, 1M). A tier passes
  only if the output matches the oracle **and** the measured CPU time is within
  that tier's budget. `perf_total = 6`.
- Exit 0 (full solve) only when GATE holds and all 6 tiers pass.

Calibrated results: shipped naive -> **0.5000**; reference -> **1.0000**. See
`checker_data/calibration.md` for the timing table.

## Anti-cheat

- **Fresh random inputs per run.** A random base seed (`SystemRandom`) is drawn
  each invocation, so answers cannot be precomputed or hardcoded, and sizes
  cannot be special-cased to a canned result.
- **Correctness on the timed run.** Each perf tier verifies the output digest on
  the *same* subprocess execution it timed — you cannot return garbage fast (see
  `.proofs/worker-perf/cheat_fast_garbage.txt`: returning `[0]*n` instantly still
  scores 0) nor trade correctness for speed.
- **Independent oracle.** Ground truth comes from a merge-sort counter
  (`checker_data/oracle.py`) — a *different* algorithm from the Fenwick
  reference — and it is self-checked against a brute-force O(n^2) pass on small
  inputs on **every** run before grading.
- **Isolation.** Each case runs in a subprocess with a wall-clock kill
  (`budget*2 + 5`s), so a runaway solver (or the naive one on a huge tier)
  cannot block or game grading. The generator and oracle are loaded by absolute
  path, so a workspace file cannot shadow them.

## Residual flakiness (honest accounting)

Wall/CPU budgets are inherently hardware-relative; this task mitigates but does
not eliminate that:

- **Clock choice.** Budgets are on `time.process_time()` (CPU time), which is
  load-independent — a busy CI box does not inflate it — and does not reward
  multi-core parallelism (we want a better complexity class, not more cores).
- **Cross-machine scaling is the real residual.** Budgets are calibrated for a
  machine within ~2x of the authoring box's single-thread speed (Apple Silicon,
  Python 3.14). Wide margins absorb a lot: the **reference stays 1.0 up to
  ~15x slower** (binding tier perf_1m: 1.0s CPU vs 20s budget), and the **naive
  baseline stays 0.5 up to ~5x slower** before its `perf_12k` pass (1.1s CPU,
  6s budget) would slip, dropping it to 0.33. A dramatically faster machine will
  not push the naive *up*: it misses `perf_100k` by ~11x, which no single-thread
  speedup in this class closes. So the naive band across plausible hardware is
  **[0.33, 0.5]**, centered on 0.5 here; the top score is stable.
- **Observed variance:** zero across 3+3 runs (0.5000 / 1.0000 exactly).
- **Recommendation:** pin the pilot to one machine class, or record the host in
  results. If ported to very different hardware, re-run
  `checker_data/calibration.md`'s reproduce steps and nudge budgets to keep the
  reference ~15x under and the naive ~5x under its last passing tier.

## Layout

```
instruction.md            the request shown to the agent (no benchmark leak)
workspace/solver/core.py  shipped NAIVE O(n^2) implementation (correct, slow)
solution/solver/core.py   golden Fenwick O(n log n) implementation
checker.sh                exec checker_data/run_score.py
checker_data/
  common.py     seeded generator + output digest (loaded by abs path)
  oracle.py     independent merge-sort ground truth + brute-force self-check
  run_tier.py   per-case subprocess: import agent solver, time it, digest output
  run_score.py  orchestrator: gate + tiers -> SCORE
  calibration.md
```
