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

## 3. Author a task

### Snapshot mode (small extracted slices)

Do **not** copy an entire monorepo into `workspace/`. The runner
`copytree`s the workspace for every trial — keep it a small extracted slice
(one package, one service stub, a minimized repro).

```bash
obench init --task demo --from path/to/small/subdir
```

That wraps the same scaffolder as `python3 -m obench.add_task`, placing the
task at `.openbench/tasks/demo/` with `subdir` copied into `workspace/`.

### Git mode (monorepos)

When the starting tree is too large to snapshot, use a `workspace.toml`
instead of `workspace/` (having both is a validation error). The runner
materializes a disposable export from a git ref for every trial — **no**
`.git` in the staged tree, and the source repo is never mutated.

```bash
obench init --task billing-bug --git-ref HEAD --git-subdir services/billing
# then edit .openbench/tasks/billing-bug/workspace.toml and pin ref to a SHA
```

Example `workspace.toml`:

```toml
kind = "git"
repo = "."                 # git repo containing the task (private-repo common case)
ref = "abc123def..."       # full commit SHA recommended
subdir = "services/billing"  # optional: only this subtree is the workspace root
# setup = "setup.sh"       # optional: task-relative script after checkout
# depth = 1                # optional: shallow clone depth for URL repos
```

**Staging choice:** OpenBench uses `git archive` (export) into the temp
workspace. That is fast, leaves no worktrees behind, never checks out or
mutates the source repo, and omits `.git` by default. Remote `repo` URLs are
cloned into a disposable temp dir, archived, then deleted.

**Setup script contract** (when `setup` is set):

- Path is task-relative (e.g. `setup.sh` next to `workspace.toml`).
- Runs with **cwd = staged workspace** and **`TASK_DIR`** pointing at the
  absolute task directory.
- Must exit 0; nonzero exit is an **infra** cell failure (not a wrong answer).

**Reproducibility:** pin `ref` to a full 40-character commit SHA. Branch or
tag names are accepted but warn at staging time; the results row records
`workspace_source.resolved_sha` so runs stay auditable.

Then (both modes):

1. Edit `instruction.md` so it reads like a normal engineering request (never
   mention the checker, scoring, or that this is a benchmark).
2. Leave the starting workspace **unsolved** (snapshot files, or the git ref
   tree before your golden fix).
3. Put the golden fix under `solution/` (only files that change or are added).
4. Implement `checker.sh`:
   - **cwd** = the temp workspace copy
   - **`$TASK_DIR`** = absolute path to the task directory
   - **exit 0** = solved; optional `SCORE: <float>` for partial credit on
     nonzero exits

### When to use which

| Mode | Use when |
|---|---|
| `workspace/` snapshot | Small fixture trees you are happy to commit into the task dir |
| `workspace.toml` git | Real monorepo slices; you already have the code at a git ref |

Docker (`--exec docker`) stages on the **host** before the container starts
(same as snapshot mode) and bind-mounts the staged tree — git mode works
there without in-container git.

> **Trust boundary:** setup and `checker.sh` execute on the host, even when the
> harness uses `--exec docker`. Local execution also runs the harness on the
> host. Only run task packs, candidate adapters, setup commands, and checkers
> that you trust and have reviewed; Docker currently isolates the harness cell,
> not the task oracle.

## 4. Polarity validation

Prove the checker fails on the untouched workspace and passes with `solution/`
overlaid:

```bash
obench validate --tasks-dir .openbench/tasks
# or, with openbench.toml defaults after init:
obench validate
```

Fix checkers until every task shows PASS.

Before sharing or installing a task publicly, run the stricter admission gate:

```bash
obench admit .openbench/tasks/my-task
```

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

## Optional: run the same tasks on Harbor

If you want Harbor’s cloud sandboxes for execution while keeping OpenBench for
comparison/stats, export your private tasks:

```bash
obench export harbor --task all --tasks-dir .openbench/tasks --out ./harbor-out
```

See [`harbor-export.md`](harbor-export.md) for the field mapping, reward-path
fallback used by local polarity checks, and known gaps (network defaults,
partial-credit mapping, checker visibility).

## What this path deliberately skips

- PyPI publication of `obench` may lag; git-URL / editable installs are the
  supported path today.
- Public leaderboard submission and originality review apply only when you
  contribute tasks upstream.
