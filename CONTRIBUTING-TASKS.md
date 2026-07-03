# Contributing a task

OpenBench tasks are small, self-contained coding problems graded by a script.
This guide is the contract for adding one. The short version: a task must be
**original**, its checker must **fail on the unsolved workspace and pass on your
golden solution**, and CI must be able to prove that offline.

## The directory contract

Create `tasks/<your-task-name>/` with exactly this layout:

```
tasks/<name>/
  instruction.md      what the harness is told — reads as a normal engineering
                      request. NEVER mention the checker, the solution, scoring,
                      or that this is a benchmark.
  workspace/          the starting files. Copied fresh into a disposable temp
                      dir for every run; the agent edits that copy. Must be in a
                      state where the checker FAILS.
  solution/           golden files overlaid on a fresh workspace to prove the
                      task is solvable. Used ONLY by validate_tasks.py — never
                      shown to the harness.
  checker.sh          grades the result. Exit 0 = solved. See below.
  checker_data/       OPTIONAL — inputs / expected outputs the checker owns,
                      kept out of workspace/ so the agent can't read the answer.
```

`solution/` is overlaid **on top of** a fresh `workspace/` copy (same file paths
replace workspace files). So `solution/` only needs to contain the files your
answer changes or adds, not the whole tree.

## The checker

`checker.sh` runs with:

- **cwd = the temp workspace copy** (never your source tree), and
- **`$TASK_DIR`** = the absolute path to your `tasks/<name>/` directory.

Reference your own data as `$TASK_DIR/checker_data/...`, never by a relative
path — the checker runs from the temp workspace, not from `tasks/<name>/`.

Keep it to tools that exist on a bare Ubuntu CI runner: **`bash` and
`python3`** (standard library) are safe. Do **not** depend on `pytest`, `node`,
`pip install`, network access, or anything that isn't preinstalled — CI has no
network and installs nothing.

Minimal example (`checker.sh`):

```bash
#!/usr/bin/env bash
# Solved iff out.txt exists in the (cwd) workspace and equals the expected file.
[ -f out.txt ] || exit 1
diff -q out.txt "$TASK_DIR/checker_data/expected.txt" >/dev/null || exit 1
exit 0
```

### Partial credit — the `SCORE:` line

A bare exit code is pass/fail. To grade partial progress (recommended for harder
tasks so a near-miss still separates harnesses), print a `SCORE:` line:

- Print `SCORE: <float 0.0–1.0>` to stdout. The **last parseable** such line
  wins; malformed values are ignored; values are clamped to `[0.0, 1.0]`.
- **Exit 0 is always a full pass** → `success=true`, score coerced to `1.0`
  regardless of any `SCORE:` line. So a fully-solved run must exit 0.
- **Nonzero exit** → `success=false`, and score = your `SCORE:` value if present,
  else `0.0`. This is how partial credit is awarded.
- A checker **timeout** records `score=0.0`.

```bash
#!/usr/bin/env bash
# 4 independent sub-checks; full pass only when all 4 hold.
pass=0
run_check_1 && pass=$((pass+1))
run_check_2 && pass=$((pass+1))
run_check_3 && pass=$((pass+1))
run_check_4 && pass=$((pass+1))
echo "SCORE: $(python3 -c "print($pass/4)")"
[ "$pass" -eq 4 ] && exit 0 || exit 1
```

`SCORE:` is optional and backward compatible: a checker that never prints one
behaves exactly as pass→1.0 / fail→0.0.

## The discipline: fail-on-workspace, pass-on-solution

Every task must satisfy two properties, both enforced by `validate_tasks.py`:

1. The checker run against a **fresh `workspace/`** (nothing solved) **must fail**
   (nonzero exit). Otherwise the task scores as solved before the agent does
   anything — a false pass.
2. The checker run against `workspace/` with **`solution/` overlaid** **must pass**
   (exit 0, and if it prints `SCORE:` it must be `1.0`). Otherwise a correct
   answer is rejected — a false fail.

This is why **expected outputs for data-driven tasks are generated *from* the
golden solution**, not hand-written: it guarantees the two ends actually agree.

Run it locally before opening a PR:

```
python3 validate_tasks.py
```

You want your task's row to read `FAIL(ok) … PASS(ok) … PASS` (workspace fails
as expected, solution passes with score 1.0).

## Original code only

**Do not copy code from other repositories, benchmarks, Stack Overflow, or model
output you haven't rewritten.** Two reasons:

- **Contamination.** If a task's code or solution is already on the public web,
  frontier models may have memorized it, and the task measures recall instead of
  capability. Original problems keep the benchmark honest.
- **Licensing.** Copied code carries its original license and authorship;
  OpenBench is MIT and every task must be your own work, contributable under that
  license.

Write the workspace, the bug/spec, and the solution yourself. Small, realistic,
self-contained problems beat large or exotic ones.

## The import tier (`tasks-imported/`)

Alongside the original `tasks/`, the repo carries a maintainer-curated **import
tier** under `tasks-imported/<collection>/` — for example tasks converted from
the MIT-licensed [Exercism problem-specifications](https://github.com/exercism/problem-specifications)
by `tools/convert_exercism.py`, which reuses only the upstream canonical test
cases (each task records its origin and license in `provenance.json`) while the
instruction prose and reference solution are written fresh. These tasks are a
**separate tier**: `validate_tasks.py` proves them like any other task but
reports them under their own tier, and the benchmark **never blends them into a
core run** (they carry a higher contamination risk since the exercises exist on
the public web, so they are scored on their own). Curating imports is a
maintainer activity; **outside contributions remain original-only** — please add
your task under `tasks/` following the rules above, not `tasks-imported/`.

## How CI checks your task

On every pull request, GitHub Actions (`.github/workflows/ci.yml`) runs, with no
network and no credentials:

1. `python3 -m unittest discover bench/tests` — the runner/report/adapter unit
   tests.
2. `python3 validate_tasks.py` — the fail-on-workspace / pass-on-solution proof
   for **every** task, including yours.

If either fails, the PR is red. No live harness or model is ever invoked in CI.

## Difficulty piloting happens after merge

CI proves your task is **well-formed and solvable** — it does **not** measure how
hard it is. Calibrating difficulty (does it separate strong from weak harnesses,
or does everyone floor/saturate?) requires paid live runs, so **maintainers pilot
new tasks post-merge** on a small harness panel and fold the result into the next
matrix. You don't need API keys or to run any harness to contribute a task; a
clean `validate_tasks.py` is the bar.

## Checklist

- [ ] `tasks/<name>/` has `instruction.md`, `workspace/`, `solution/`, `checker.sh`.
- [ ] `instruction.md` reads as a normal request; no mention of checker/solution/benchmark.
- [ ] Checker uses only `bash` + `python3` stdlib; references data via `$TASK_DIR`.
- [ ] All code is original (no copied/licensed material).
- [ ] `python3 validate_tasks.py` shows your task `FAIL(ok) … PASS(ok) … PASS`.
- [ ] `python3 -m unittest discover bench/tests` passes.

See [`README.md`](README.md#task-format) for the runtime contract and
[`validate_tasks.py`](validate_tasks.py) for the exact validation logic.
