# Calibration — perf-budget

Budgets are on **CPU time** (`time.process_time()` of the solver call only),
measured on the authoring machine:

- Apple Silicon (macOS 25.4, `darwin` arm64), Python 3.14.4, single thread.

CPU time is used instead of wall time so the check is stable under machine load
(a busy CI box inflates wall time but not on-CPU time) and so throwing extra
cores at an O(n^2) solver does not buy a pass — only a better complexity class
does.

## Measured times (seconds of CPU), mode = uniform

| Tier      |       n | naive O(n^2) | reference O(n log n) | oracle (merge) | budget | naive | ref margin |
|-----------|--------:|-------------:|---------------------:|---------------:|-------:|:-----:|-----------:|
| perf_2k   |   2,000 |        0.031 |                0.001 |          0.001 |   1.0s | PASS  |   ~1000x   |
| perf_8k   |   8,000 |        0.49  |                0.004 |          0.006 |   3.0s | PASS  |    ~750x   |
| perf_12k  |  12,000 |        1.10  |                0.006 |          0.010 |   6.0s | PASS  |   ~1000x   |
| perf_100k | 100,000 |      ~90     |                0.063 |          0.106 |   8.0s | FAIL  |    ~127x   |
| perf_350k | 350,000 |    ~1100     |                0.286 |          0.465 |  12.0s | FAIL  |     ~42x   |
| perf_1m   | 1,000,000 |    huge     |                1.02  |          1.73  |  20.0s | FAIL  |     ~19x   |

`naive` times for the three large tiers are extrapolations from the O(n^2)
curve (they are never run to completion — the checker kills each case at
`budget*2 + 5` s of wall time).

## Score at each end (calibrated, and observed 3x each)

- **Shipped naive workspace** — clears the correctness gate and the first three
  tiers, killed on the last three: `gate PASS`, `perf 3/6` -> **SCORE 0.5000**.
- **Reference solution** (Fenwick / BIT, `solution/`) — clears everything:
  `gate PASS`, `perf 6/6` -> **SCORE 1.0000**.

Observed variance across 3 pristine + 3 solution runs: **none** — 0.5000 and
1.0000 exactly (see `.proofs/worker-perf/`). This is expected: CPU time is
load-independent, and every tier sits far from its budget boundary.

## Headroom / boundary gaps (why it is not flaky)

- Reference is **19x-1000x under budget** at every tier — comfortably robust to
  a CI box up to ~15x slower than the authoring machine before `perf_1m`
  (the binding tier: 1.02s CPU vs 20s budget) would start to risk it.
- The naive solver clears its last passing tier (`perf_12k`, 1.1s) with **~5x
  headroom** and misses the next (`perf_100k`) by **~11x** (killed long before
  finishing). Nothing sits in the ambiguous zone, so the 0.5 baseline does not
  wander with seed or machine noise.

## Reproduce

```
cd tasks-candidates/perf-budget
cp -r workspace /tmp/ws && cd /tmp/ws
TASK_DIR=<abs path to task> bash "$TASK_DIR/checker.sh"     # -> 0.5000
cp -r "$TASK_DIR/solution/"* . && TASK_DIR=<...> bash "$TASK_DIR/checker.sh"  # -> 1.0000
```
