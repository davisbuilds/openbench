# am-consistency-pr80 — terra vs luna (2026-08-29)

Task: `tasks/am-consistency-pr80` (K=3, effects-only instruction). A graded,
long-context bug-fix task built from real Codex PR-review findings on
davisbuilds/agentmonitor PR #80 — three cross-site-consistency defects planted in
the 94-file `src/` haystack, symptoms only (no mechanism, no locations), scored
`fixed/3`. Native codex, serial, 3 trials/arm. Terra/luna sanity pass before
spending on the open arms.

## Result — accuracy ties, effort diverges hard

Both frontier arms scored **1.0 (3/3 fixed) on every trial.** The difference is
efficiency, and it is large:

| per trial | terra-xhigh | luna-max | luna/terra |
|---|---|---|---|
| score | 1.000 | 1.000 | — |
| wall time | 354s | 851s | 2.4× |
| fresh input tok | 89K | 230K | 2.6× |
| cache-reads (context re-feeds) | 1.2M | 8.1M | **6.7×** |
| output tok | 23K | 55K | 2.4× |

## Interpretation

- The defect class (cross-site consistency, the dominant real finding in this
  workspace) is **within both frontier arms' capability** — both diagnose from
  symptoms alone and fix all three.
- The divergence is **long-context efficiency**: luna re-reads the codebase 6.7×
  more and runs 2.4× longer to reach the same fix. The cache-read ratio is the
  MRCR long-context-recall gap (terra 90 vs luna 41) made concrete — luna can't
  hold the 94-file context, so it thrashes across many more turns.
- **Difficulty amplifies it.** The earlier K=2 draft *named the mechanism* in the
  instruction; both arms scored 1.0 and luna's total-input gap was ~2.7×.
  Rewriting to effects-only (forcing diagnosis) blew the cache-read gap to 6.7× —
  because diagnosis-under-long-context is precisely luna's weak spot.

## Pareto implication

luna's ~10× cheaper per-token price is substantially eroded by its 6.7×
token inefficiency on hard long-context work, and it is 2.4× slower. So terra's
premium is more justified on hard bug-fix work than the sticker price implies;
luna's cost advantage holds only on tasks easy enough that it does not thrash.
(Codex-native runs on subscription, so cost here is list-price theoretical.)

## Status

Frontier calibration done: the task is well-formed and frontier-solvable with a
clear effort gradient. Next: the 4 open arms (minimax-m3, laguna-s-2.1,
glm-5.3-flash, deepseek-v4-flash-0731) via the bridge, where accuracy spread is
expected — completing the score×cost Pareto ladder. See
`experiments/specs/am-consistency.toml`.
