# Provenance — am-consistency-pr80

A graded, long-context bug-fix task built from **real** Codex PR-review findings,
to test the dominant real-world defect class in this workspace (cross-site
consistency) on the axis where frontier models diverge most (long-context recall).

## Source

- Repo: `davisbuilds/agentmonitor` (the user's own public TypeScript project).
- PR: **#80** "Decompose skill consultation telemetry by harness", merged
  2026-07-28. Codex (`chatgpt-codex-connector[bot]`) posted 3 findings (1 P1,
  2 P2); this task uses two of them (K=2):
  - **F2 (P2)** `src/parser/codex-sessions.ts` — "Preserve contiguous Codex
    content without inserted bytes." Fragment join used `'\n'` instead of `''`.
  - **F1 (P1)** `src/skills/consultation-analytics.ts` — "Classify against the
    full session history." Windowing the classification input misreports the
    first in-window repeat as `first_read`.
- Ground-truth fixes and regression tests are the user's own, authored in the
  same PR (commits `59f0d87`, `8209087`). The two hidden tests in
  `checker_data/` are lifted verbatim from that history.

## Construction

- Base tree: agentmonitor HEAD (`eebf992`, the fixed state), materialized in a
  disposable git worktree. The canonical repo was never modified.
- Each defect was reintroduced surgically into the fixed tree and verified to
  break **only** its own regression test: with both defects applied, exactly
  2 of the full 770-test suite fail (the two target tests); the oracle
  (`solution/`, the fixed files) passes both.
- `workspace/` ships the full `src/` haystack (94 `.ts` files, ~1.0M) with both
  defects, plus `package.json` + `tsconfig.json`. It intentionally omits
  `node_modules` and `tests/` so the agent must locate the defects by reading the
  code, not by reading a failing test.

## Scoring

Graded. `checker.sh` symlinks a canonical `node_modules` read-only (host, local
exec), drops the two hidden tests into `tests/`, and runs each finding's target
test by name. `SCORE = fixed/2` (0, 0.5, 1.0); exit 0 iff both pass.

## Contamination

Low: the agent receives the *buggy* state, not the fix; the fix post-dates the
models' training window (PR merged 2026-07-28); and the reintroduced F1 mechanism
differs from the original source formulation, so a memorized diff would not apply
cleanly. The regression tests assert *behavior*, so any correct fix scores.

## Caveats / portability

- **Fork-local, machine-specific.** The checker needs a prebuilt agentmonitor
  `node_modules` (native `better-sqlite3` for this host's node) at
  `/Users/dg-mac-mini/Dev/agentmonitor/node_modules` or `$AGENTMONITOR_DEPS`.
  Not portable to CI without vendoring deps or a docker image.
- **Local exec only** as written (checker provisions deps on the host). The task
  is the user's own trusted code, so local mode is acceptable here.
