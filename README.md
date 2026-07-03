# OpenBench

A benchmark for comparing coding-agent **harnesses** — the CLI tools that wrap a
model in a run loop, tool set, and permission policy (codex, pi, opencode,
cursor, devin). The question it answers is: *given the same underlying model,
how much does the harness around it matter?*

**New here?** [`WRITEUP.md`](WRITEUP.md) tells the whole story — the question, the method, and the findings arc (M3 → M4) — for a reader who's never seen the repo.

## What this measures

Each harness runs headlessly against a set of small, self-contained coding
tasks. A task is graded by a checker script (exit 0 = solved), never by the
harness's own claim of success. Two framings:

- **Track A — same model, harness varies.** Every harness is pinned to the same
  canonical model, `gpt-5.5-medium`, so differences in the results come from the
  harness (its scaffolding, tools, and prompting), not the model.
- **Track B — native stacks.** Each harness runs on the model/config it is
  actually shipped and tuned for. This measures the product as users experience
  it, and is not directly comparable across harnesses.

**Honest caveat (Track A):** `devin` exposes no reasoning-effort selector, so the
canonical `gpt-5.5-medium` collapses to plain `gpt-5.5` at whatever effort devin
defaults to. Its effort is therefore **unpinned** — treat devin's Track A number
as approximate, not a like-for-like comparison with the other harnesses.

## Layout

```
tasks/                 benchmark tasks (see "Task format")
validate_tasks.py      proves every task's checker is correctly polarized
bench/run.py           the runner (one row per task x harness x trial)
bench/report.py        aggregates results into a table with Wilson CIs
bench/adapters/*.py    one adapter per harness (+ built-in "null" control)
bench/ADAPTER_SPEC.md  the adapter contract
bench/entry.py         in-container entrypoint for --exec docker
bench/docker_exec.py   container-per-cell execution backend
bench/docker/          isolation image for --exec docker
results/results.jsonl  append-only results log (gitignored)
```

## Quickstart

Everything is Python 3 standard library only — no dependencies to install.

**1. Validate the tasks.** Confirms each checker fails on the untouched
workspace and passes on the golden solution (see "Task format"):

```
python3 validate_tasks.py
```

**2. Preflight.**

```
python3 bench/doctor.py
```

For each harness it checks — spending no tokens — that the CLI is installed, its
auth/login is present, and the canonical model pin resolves to the harness's own
model string. A failing preflight exits nonzero.

**3. Run.** Pick harnesses and tasks. Start with the zero-cost `null` control to
confirm the plumbing, then add real harnesses:

```
# negative control — does nothing, so every task should fail (no tokens used)
python3 bench/run.py --harness null --task fix-failing-test,build-a-cli,make-it-run

# a real harness, 3 trials per task
python3 bench/run.py --harness codex --task fix-failing-test,build-a-cli,make-it-run --trials 3
```

Multiple harnesses/tasks are comma-separated. The run loop is **resumable**: a
cell whose `run_id` already appears in `results/results.jsonl` is skipped, so you
can stop and re-run freely. Use `--force` to re-run existing cells. Full options:
`python3 bench/run.py --help`.

**4. Report.**

```
python3 bench/report.py
```

Example output (from the `null` control on two tasks):

```
harness  fix-failing-test  make-it-run  overall   wilson95        mean_s  tokens
-------  ----------------  -----------  --------  --------------  ------  ------
null     0/1               0/1          0/2 (0%)  [0.000, 0.658]  0.00    -
```

## Task format

A task is a directory under `tasks/<name>/`:

```
instruction.md      what the harness is told (reads as a normal engineering request)
workspace/          starting files; copied fresh into a temp dir for every run
checker.sh          grades the result; exit 0 = solved
solution/           golden files, used ONLY by validate_tasks.py (never shown to the harness)
checker_data/       optional: inputs/expected outputs the checker owns (kept out of workspace/)
```

Contract the runner honors for every cell:

- `workspace/` is copied to a disposable temp dir; the harness edits that copy.
  The source under `tasks/` is never modified.
- `checker.sh` runs with **cwd = the temp workspace copy** and the environment
  variable **`TASK_DIR`** set to the absolute task directory. Checkers reference
  their own data via `$TASK_DIR/checker_data/...` rather than a relative path, so
  they work regardless of cwd.
- Instructions never mention the checker, the solution, or that this is a
  benchmark.

#### Partial credit (the `SCORE:` contract)

A checker exit code is binary, but a checker MAY also emit a **`SCORE:`** line to
grade partial progress. The rules the runner applies:

- A checker may print `SCORE: <float 0.0–1.0>` to stdout. The **last parseable**
  such line wins; a malformed value is ignored (as if that line were absent), and
  values are clamped to `[0.0, 1.0]`.
- **Exit 0 is always a full pass** — `success = true` and `score` is coerced to
  `1.0` regardless of any `SCORE:` line.
- **Nonzero exit** — `success = false`, and `score` is the checker's `SCORE:`
  value if present, else `0.0`. This is how a task awards partial credit.
- A checker **timeout** records `score = 0.0`.

`SCORE:` is optional and backward compatible: a checker that never prints one
behaves exactly as before (pass → 1.0, fail → 0.0).

### Validation discipline

`validate_tasks.py` enforces that each checker is correctly polarized:

1. Run the checker against a fresh copy of `workspace/` → it **must fail**
   (otherwise the task is scored solved before the agent does anything).
2. Run it against `workspace/` with `solution/` overlaid → it **must pass**
   (otherwise a correct answer would be rejected).

This catches the two ways a checker can silently lie about difficulty, and is
why expected outputs for data-driven tasks are generated *from* the golden
solution rather than written by hand. The bundled tasks are `fix-failing-test`
(fix a planted bug so the unit tests pass), `build-a-cli` (write a word-frequency
CLI to a precise spec), and `make-it-run` (repair a small broken project).

## Adapters

Each harness is a module `bench/adapters/<name>.py` exposing `NAME`, a `MODELS`
map, and `run(instruction, workdir, model, timeout_s) -> dict`. The adapter maps
the canonical model name to the harness's own flags, runs the CLI headlessly with
cwd = `workdir`, enforces the timeout via `subprocess` (no `timeout` command —
macOS has none), and returns `completed` / `error` / `tokens` / `turns` / `cmd`.
`completed` means the CLI exited cleanly; it is **not** task success — the checker
decides that. Full contract: [`bench/ADAPTER_SPEC.md`](bench/ADAPTER_SPEC.md).

Auth is handled inside each adapter, read-only — the user's real config files are
never modified:

| Harness  | Canonical `gpt-5.5-medium` maps to        | Auth handling                                                                 |
|----------|-------------------------------------------|-------------------------------------------------------------------------------|
| codex    | `gpt-5.5`, `model_reasoning_effort=medium`| Uses the existing `codex` login as-is                                          |
| pi       | `gpt-5.5`, `--thinking medium`            | Isolated `HOME` (temp dir) with only `.pi/agent/auth.json` copied in, plus `--no-extensions`, so personal extensions never load |
| opencode | `openai/gpt-5.5`, `--variant medium`      | Strips `OPENAI_API_KEY` from the child env to force the subscription OAuth credential |
| cursor   | `gpt-5.5-medium` (effort baked into name) | Uses the existing `cursor-agent` login as-is                                   |
| devin    | `gpt-5.5` (**no effort selector**)        | Uses the existing `devin` login; reasoning effort unpinned (see caveat above) |

The built-in `null` adapter does nothing and reports `completed=True`. Because it
never edits the workspace, every task's checker fails — it is the benchmark's
**negative control**, and uses no tokens.

## Results

Findings from the first full run are in [`RESULTS.md`](RESULTS.md) (M3 matrix, 2026-07-02).

`bench/run.py` appends one JSON object per line to `results/results.jsonl`. The
fields:

| Field          | Meaning                                                                 |
|----------------|-------------------------------------------------------------------------|
| `run_id`       | `harness:task:model:trialN` — the resumable identity of the cell        |
| `ts_iso`       | local timestamp when the cell ran                                       |
| `harness`      | adapter name (or `null`)                                                |
| `model`        | canonical model name (default `gpt-5.5-medium`)                         |
| `task`         | task directory name                                                     |
| `trial`        | 1-based trial index                                                     |
| `success`      | **the graded result** — `checker_exit == 0`                            |
| `completed`    | harness CLI exited cleanly (self-reported; not success)                 |
| `error`        | timeout / crash / adapter exception, else `null`                        |
| `wall_time_s`  | adapter wall-clock seconds                                              |
| `tokens`       | tokens reported by the harness, else `null`                            |
| `turns`        | turns reported by the harness, else `null`                             |
| `cmd`          | the command line executed (for auditability)                            |
| `checker_exit` | checker's integer exit code, or `"timeout"`                            |
| `exec_mode`    | `local` or `docker` (what actually ran, after any fallback)             |
| `score`        | graded score in `[0.0, 1.0]` (see the `SCORE:` contract); `1.0`/`0.0` for a plain pass/fail |
| `harness_version` | version string from the adapter's optional `version()`, `"builtin"` for `null`, else `null` |

Rows written before `score`/`harness_version` existed simply omit them; the
report derives a score from `success` (`1.0`/`0.0`) for those.

`bench/report.py` reads that log and prints one row per harness: per-task
success (`x/n`), overall success with a Wilson 95% interval, **mean score**
(averaged over all trials, the discriminating number for partial-credit tasks),
mean wall-clock time, tokens-per-solve, and mean turns. `--efficiency` prints a
per-harness efficiency summary; `--results-path` points it at an alternate log.

### Reading the Wilson interval

Every success rate here is estimated from a handful of trials, so the point
estimate (say "2/3 = 67%") is noisy. The **Wilson 95% confidence interval** is
the range of true success rates consistent with what was observed — we are about
95% confident the harness's real success rate lies inside it. Two things to keep
in mind: with few trials the interval is **wide** (3 trials can easily span most
of 0–100%), so overlapping intervals mean the harnesses are *not* distinguishable
yet; and the interval shrinks as trials grow. Wilson (rather than the textbook
`p ± z·√(p(1−p)/n)`) is used because it stays inside `[0, 1]` and behaves sensibly
at 0/n and n/n, which the naive formula does not.

## Methodology & limitations

- **Same-model pinning (Track A).** All harnesses target `gpt-5.5-medium` so the
  harness is the variable — with the devin exception noted above.
- **Fresh workspace per run.** Every cell gets an untouched copy of the task
  workspace; runs cannot contaminate each other or the source tree.
- **Checker is the sole judge.** Success is `checker.sh` exit 0, never the
  harness's self-report. `validate_tasks.py` guarantees each checker actually
  discriminates a solved workspace from an unsolved one.
- **Negative control.** The `null` adapter should score 0% everywhere; a nonzero
  `null` success would indicate a broken (too-lenient) checker.
- **Isolation modes.** `--exec local` (default) runs on the host; `--exec docker`
  runs each cell in a fresh disposable container built from `bench/docker/`
  (`docker build -t openbench-harness:latest bench/docker`). The **same adapter
  module runs unchanged** in both modes — the container only adds isolation, with
  auth bind-mounted read-only at runtime (never baked into the image). If the
  Docker daemon or image is unavailable, the runner falls back to local unless
  `--no-docker-fallback` is set.
- **Docker image is partial.** Only `codex` and `pi` are installed and
  version-checked in the default image; `opencode`, `cursor`, and `devin` are
  behind `--build-arg INSTALL_UNVERIFIED=true` and their Linux installs are not
  yet confirmed. For those harnesses, use `--exec local` for now.
- **Sample size.** These are small tasks in small numbers; the current results
  are a plumbing/shakedown sample, not a verdict. Read the Wilson intervals, not
  the point estimates, and treat cross-harness gaps as real only when the
  intervals separate.

## License

OpenBench is available under the MIT License. See [LICENSE](LICENSE).
