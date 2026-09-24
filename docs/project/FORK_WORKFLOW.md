# Project and upstream workflow

OpenBench is maintained as a standalone checkout. The canonical trunk is
`davisbuilds/openbench`; `minghinmatthewlam/openbench` is an upstream source for
selective integrations and focused contributions. Our roadmap and releases do
not wait for upstream. Changes flow **local → our trunk → upstream when broadly
relevant**.

The checkout was promoted to a standalone project on 2026-09-16. Linked
worktrees and editable tool environments were repaired; an existing sealed
suite manifest verified after relocation. Historical local run records retain
their original paths as provenance. Start new runs from the current checkout
rather than replaying old absolute launcher commands.

## Remotes

| Remote   | URL                                    | Role                                    |
|----------|----------------------------------------|-----------------------------------------|
| `fork`   | `github.com/davisbuilds/openbench`     | **Your trunk.** `main` tracks `fork/main`. |
| `origin` | `github.com/minghinmatthewlam/openbench` | Upstream. Pull from occasionally; PR into it. |

`main` tracks `fork/main`, so `git push` / `git pull` move against **your fork**,
not upstream. Set `remote.pushDefault = fork` for topic branches too. Remote
names are retained to keep existing branch tracking clear. Upstream is a source
you integrate *from* and open PRs *to*, never a push target for your trunk.

## The flow

1. **Local first.** Build on a topic branch (or directly on `main` for
   fork-local convenience). Red/green per the working agreements; keep commits
   coherent.
2. **Push to your fork.** `git push` sends `main` to `fork/main` — your backup
   and canonical trunk. Your fork is *expected* to diverge from upstream; that is
   part of this project's independent development.
3. **Promote upstream only if broadly relevant.** Capability that benefits every
   openbench user goes upstream as a **clean topic branch cut from `origin/main`**
   carrying *only* that feature's commits — never your whole divergent `main`.
   For substantial capabilities, propose a scoped issue before preparing a large
   PR. Small reproduced compatibility/correctness fixes can go directly to a
   focused PR. Do not send our divergent trunk wholesale.

### What goes upstream vs. what stays fork-local

- **Upstream candidates**: general runner/harness/adapter capability. Tracked as
  upstream issues/PRs (see `BACKLOG.md`). Examples in flight: failure-class
  classification (PR #43), matrix-runner timeout/wall-cap (PR #47).
- **Fork-local, never upstream**: experiment specs (`experiments/specs/*`),
  private model wiring / bake-off routes, `BACKLOG.md`, this doc. These are our
  operating context, not shared capability.

A feature is only ready to promote once it stands on its own — e.g. arm-level
`--workers` (issue #45) waits on per-arm bridge isolation; the open-model config
registry (issue #46) waits on schema generalization. Until then it lives on the
fork.

## Where fork-local content lives (tree ownership)

The flow above governs *commits*; this governs the *tree*. The rule that keeps
upstream promotion clean by construction:

> **Fork-local content lives in fork-owned locations. Never add it to an
> upstream-owned directory — each upstream-owned dir has a fork-owned sibling.**

If `tasks/` stays byte-identical to upstream, you never have to remember to
strip local tasks out of a promotion branch — there is nothing local in there to
strip.

| Concern            | Upstream-owned (keep clean)      | Fork-owned (never upstream)                 |
|--------------------|----------------------------------|---------------------------------------------|
| Benchmark tasks    | `tasks/` (core), `tasks-imported/` | `tasks-local/` (the fork-local task tier)   |
| Native Harbor task packages | `harbor-tasks/` | `harbor-tasks-local/` (fork calibration/admission) |
| Experiment specs   | —                                | `experiments/specs/*`                       |
| Model wiring       | adapter contracts in `obench/`   | `open_models_config.py`, bake-off routes    |
| Runner/harness     | `obench/` capability             | fork-local convenience commits on `main`    |
| Project docs       | `docs/*` (feature docs)          | `docs/project/*` (this doc, `BACKLOG.md`)   |

Two guards enforce the tasks row so it does not decay back into tribal
knowledge:

- **`obench validate` tiers.** Task discovery walks three tiers: `core`
  (`tasks/`), `imported` (`tasks-imported/`), and `local` (`tasks-local/`). A
  fork-local task goes in `tasks-local/`; a spec points its task group at it with
  `tasks_dir = "../../tasks-local"`.
- **Environment-gated SKIP.** A fork-local checker that needs host deps absent in
  CI (or on another machine) exits **77**; `obench validate` reports **SKIP**,
  not FAIL, and never a faked PASS. So CI exercises the portable local tasks and
  honestly skips the rest. (`tasks-local/am-consistency-pr80` is the worked
  example — it needs a host-native agentmonitor `node_modules`.)
- **Portability tripwire.** `tests/test_core_task_portability.py` fails if any
  `tasks/` checker hard-codes a machine-specific path or a fork-local dep var, so
  a local task can never silently re-enter core.

## Cutting a clean upstream branch from divergent `main`

Because `main` bundles upstream-worthy commits *with* fork-local convenience,
promote by isolating the former:

```sh
git fetch origin
git switch -c feat/<thing>-upstream origin/main
git cherry-pick <sha>...            # only the feature's commits
git push fork feat/<thing>-upstream # PR this branch into origin
```

Keep the topic branch alive on `fork` until its PR merges or closes.

## Bringing in useful upstream changes

Fetch for inspection; integrate on a topic branch, never by resetting our trunk:

```sh
git fetch origin
git log --oneline fork/main..origin/main
git diff --stat fork/main...origin/main
git switch -c integrate/<topic> fork/main
# Cherry-pick a self-contained upstream fix, or merge a reviewed upstream range.
```

Review dependencies and overlapping local implementations before selecting a
commit. If upstream has merged one of our PRs, compare the resulting behavior
and tests with the local copy before resolving duplicates. Run the owning
checks and open a PR into our trunk. Preserve published history: routine
upstream reconciliation does not rebase or force-push `fork/main`.

There is no automatic upstream pull cadence. Inspect upstream when a useful fix
or feature appears, or during periodic maintenance, and bring in only changes
whose value and compatibility have been reviewed.

## Dangling branches

A topic branch fully merged into `main` (`git branch --merged main`) that backs
no open PR is dead weight — delete it (`git branch -d`, which only succeeds when
it is fully merged, so that is its own safety check). A branch backing an open
upstream PR stays until the PR merges or closes.

## CI ownership

The fork maintains its own [CI coverage and workflow baseline](CI.md), including
credential-free pinned Harbor/Docker integration. Preserve portable upstream
contracts while adding controls for the execution paths maintained here.
