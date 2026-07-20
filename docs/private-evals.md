# Private evaluations on your own codebase

OpenBench is designed for companies that want to compare coding-agent harnesses
on **their** tasks — not only the public synthetic and imported tiers. This
on-ramp gets a private repo from install to a first matrix in about fifteen
minutes.

**Scope note:** Private tasks under `.openbench/tasks/` are **exempt** from the
public-contribution originality and contamination rules in
[`CONTRIBUTING-TASKS.md`](../CONTRIBUTING-TASKS.md). Those rules apply when you
contribute tasks to the shared public tiers (`tasks/`, `tasks-imported/`). Keep
private code, transcripts, and results inside your repo’s ignore rules.

## 1. Install

Python 3.11+. Stdlib only for the framework itself.

```bash
# from a clone
pip install -e /path/to/openbench

# or without cloning first
pip install "git+https://github.com/minghinmatthewlam/openbench.git"
```

Confirm the CLI:

```bash
obench --version
```

Legacy `python3 bench/...` shims still work with a deprecation note; prefer
`obench ...`.

## 2. Scaffold in your repo

From the root of the private codebase (any git repo):

```bash
obench init
```

This creates `.openbench/` with:

| Path | Purpose |
|---|---|
| `openbench.toml` | Optional defaults (`tasks_dir`, `results_path`, harnesses/model/trials) |
| `tasks/` | Your private tasks (includes a commented `example/` skeleton) |
| `results/` | Local results log (gitignored) |
| `.gitignore` | Ignores `results/` and `transcripts/` under `.openbench/` |

Re-running `obench init` is idempotent: existing files are skipped and noted.

`obench run`, `obench report`, and `obench validate` read
`.openbench/openbench.toml` from the current directory or the nearest ancestor.
**Explicit CLI flags always win** over the config file.

## 3. Author a task from a code slice

Do **not** copy an entire monorepo into `workspace/`. The runner
`copytree`s the workspace for every trial — keep it a small extracted slice
(one package, one service stub, a minimized repro).

```bash
obench init --task demo --from path/to/small/subdir
```

That wraps the same scaffolder as `python3 -m obench.add_task`, placing the
task at `.openbench/tasks/demo/` with `subdir` copied into `workspace/`.

Then:

1. Edit `instruction.md` so it reads like a normal engineering request (never
   mention the checker, scoring, or that this is a benchmark).
2. Leave `workspace/` in the **unsolved** starting state.
3. Put the golden fix under `solution/` (only files that change or are added).
4. Implement `checker.sh`:
   - **cwd** = the temp workspace copy
   - **`$TASK_DIR`** = absolute path to the task directory
   - **exit 0** = solved; optional `SCORE: <float>` for partial credit on
     nonzero exits

## 4. Polarity validation

Prove the checker fails on the untouched workspace and passes with `solution/`
overlaid:

```bash
obench validate --tasks-dir .openbench/tasks
# or, with openbench.toml defaults after init:
obench validate
```

Fix checkers until every task shows PASS.

## 5. Doctor preflight (no token spend)

```bash
obench doctor --harness pi,opencode --model gpt-5.5-medium
```

Checks CLI install, auth/key presence, and model pin resolution. Use `null`
for a zero-cost plumbing check (no external CLI).

## 6. Run a matrix

Smoke the plumbing:

```bash
obench run --harness null --task demo
obench report
```

Then real harnesses (CLIs must be installed and authenticated — see
[`SETUP.md`](../SETUP.md)):

```bash
obench run --harness pi,opencode --task demo --trials 3
obench report
```

Config-file defaults mean you typically omit `--tasks-dir` and
`--results-path` after `obench init`. For large arms, prefer
`--preflight-smoke` so a near-zero-token infra failure refuses the run before
burning the matrix; smoke prefers `make-it-run` when present, otherwise the
first runnable task under the resolved tasks directory.

## 7. Read the report (Wilson CIs)

`obench report` prints per-harness success rates with Wilson 95% confidence
intervals, plus an efficiency summary (wall time / tokens / turns per solve).
Treat wide intervals as “not enough trials yet,” not as a ranking.

## 8. Transcripts stay local

Raw transcripts land next to the results log (default:
`.openbench/results/transcripts/` after init). They are **unscrubbed** and may
contain paths, secrets, or customer strings. Before sharing:

```bash
python3 -m obench.scrub .openbench/results/transcripts/ --check
python3 -m obench.scrub .openbench/results/transcripts/ --out scrubbed/
python3 -m obench.scrub scrubbed/ --check
```

## What this path deliberately skips

- Git-ref workspace materialization (clone a ref + setup script instead of a
  static `workspace/` snapshot) is a separate roadmap item — not required for
  small extracted slices.
- PyPI publication of `obench` may lag; git-URL / editable installs are the
  supported path today.
- Public leaderboard submission and originality review apply only when you
  contribute tasks upstream.
