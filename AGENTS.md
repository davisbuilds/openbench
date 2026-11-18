# OpenBench — Agent Context

OpenBench compares coding-agent **harnesses** (codex, pi, opencode, cursor,
devin, claude, ...): the CLI products that wrap a model in a run loop, tool set,
and permission policy. Tasks are self-contained (`task.toml` + `instruction.md`
+ Docker environment + verifier); the verifier is the sole judge.

## Documentation map

- [`README.md`](README.md) and the [documentation index](docs/README.md):
  product overview, guides, and reference.
- [Roadmap](docs/project/ROADMAP.md): product goals, landscape position, and
  priorities. [Backlog](docs/project/BACKLOG.md): unselected future work.
- [Project and upstream workflow](docs/project/FORK_WORKFLOW.md): remotes, PR
  flow, upstream promotion, and fork-local tree ownership.
- [Repository layout](docs/project/REPOSITORY_LAYOUT.md): asset ownership, the
  code map, and path compatibility.
- [CI baseline](docs/project/CI.md): workflows, lanes, and stable job names.

## Local execution context

This checkout's `fork` remote is the primary trunk;
`origin` remains the upstream source for selective integrations. See
[project and upstream workflow](docs/project/FORK_WORKFLOW.md).

Before changing benchmark execution or starting a run, read `agents.env` when
it exists. It is a gitignored, machine-local source of truth for where code is
developed and where benchmarks are executed. Never commit it or put credentials
in it.

For this installation, code changes belong in the laptop checkout. Benchmark
runs normally execute on the Mac Mini from an exact pushed commit. Do not edit
source on the Mini, do not launch from a dirty or stale checkout, and check for
active benchmark processes before starting another run.

Launch every multi-trial campaign inside a named `tmux` session on the execution
host, with persistent logs and a completion/exit-code receipt. Do not substitute
`nohup` or a detached Python process. Verify the live session and runner after
launch, and report the session name, host, result path and next status check.
Follow [the campaign launch and closeout checklist](docs/benchmark-operations.md).
When AgentMonitor is configured, reconcile scored results into its running dashboard
and verify study/cell counts before calling delivery complete; session watching
does not import benchmark files. For
checkout-based isolated repair campaigns, use `obench campaign qualify` then
`obench campaign launch --admission ... --quality ...`; the wrapper supervises canonical
`obench run` and rejects missing or stale runtime admission. Repair campaigns also require one current task-quality receipt per task; see
[repair validation](docs/repair-validation.md). Inspect with
`obench campaign status`. Direct `obench run` remains the control/diagnostic and
verified Harbor-resume entry point.

`tmux` survives terminal/SSH disconnects, not system sleep or reboot. On macOS,
wrap the runner with `caffeinate -i` and verify its sleep assertion. For laptop
runs, keep AC power connected and the lid open; do not promise that caffeinate
prevents lid-triggered sleep. Prefer the Mini for unattended runs when the
laptop may be closed, after provisioning the exact pushed commit and runtime.
After unexpected sleep, inspect the ledger, transcripts and power events before
resuming; preserve original attempts and flag affected timing/timeout evidence.

PR-derived difficulty calibration additionally requires an enforced read
boundary: agents must not see hidden verifiers, reference solutions, sibling
worktrees, source history or earlier trial transcripts. Native `workspace-write`
and HOME isolation do not establish that boundary. Before model trials, prove
allowed workspace reads/writes and denied direct/symlink/subprocess reads of
known-present host canaries through the actual execution route. Keep verifier
assets out of the agent image. Terminate all solver processes before artifact
extraction; grade in a separate environment, with oracle and reward authority
outside candidate code. An exited harness command is not proof that background
processes stopped. Require effective denial of network answer retrieval too,
including SSH/tailnet/host services and provider-side search. The selected
[repair sandbox](docs/project/REPAIR_SANDBOX.md) is opt-in via suite `[sandbox]`
with offline runtime/trial controls, passing short authenticated controls, and
a verified 600-second Luna timeout with complete evidence and atomic suite
import. Renew controls when runtime or adapter bytes change. These checks do
not establish difficulty calibration or admit an unattended campaign.
Treat scores from an unproven boundary as diagnostic, not golden-set evidence.

Before authoring or revising review/repair evaluations, read
[task fidelity](docs/review-task-fidelity.md). Check the actual exported review
scope, Git comparison, ordinary instructions/tests and offline tool workflows;
package validity and checker polarity do not establish realism.

## Execution ownership

The canonical path is `obench run [suite.toml]`. Pinned Harbor owns task/trial
execution, Docker sandboxes, concurrency, retries, resume, locks, verifier
execution, and ATIF artifacts. OpenBench owns immutable suite intent, profiles,
one exact Harbor job per task set, comparison-plan sidecars, atomic import of
every intended job, proxy reconciliation, comparison, statistics, publication
policy, and site acceptance. `obench legacy run` is compatibility only; manual
Harbor export, job-run, and result-import commands are diagnostics and migration
tools, not the default workflow.

## Local gate (matches `.github/workflows/ci.yml`)

```bash
pip install -e .
python3 scripts/ci/unit_tests.py   # --workers 0 runs the serial reference
obench validate --no-imported
obench validate --tasks-dir benchmarks/imported/terminal-bench
obench validate --tasks-dir benchmarks/imported/exercism
obench validate --tasks-dir benchmarks/local
```

Offline and stdlib-only: no live harness or model-API calls. Docker-backed
imported tiers, including Terminal-Bench 2, and the pinned Harbor integration
lanes run in separate workflows. Workflow changes follow the
[CI baseline](docs/project/CI.md): pinned action SHAs and runner versions,
bounded jobs, minimal permissions, superseded-run cancellation, frozen
dependencies, and a workflow self-audit.

## Guardrails

- The checker is the sole judge of success; never trust harness self-reports.
- Every new task must pass `obench validate` polarity (fails untouched,
  passes with `solution/` overlaid).
- Transcripts are local-only and never published unscrubbed (`obench/scrub.py`).
- Committed datasets live under `data/`; local scratch stays in gitignored
  `results/`.
- Non-goals: no OpenBench-owned task scheduler, sandbox runtime, cloud backend,
  or RL training story (Harbor owns execution; OpenBench owns intent and
  evidence). Keep the stdlib-only, files-plus-shell-checker task contract; it
  is the accessibility edge.

### Dangerous zones

- **Repair revisions** — follow [benchmark versioning](docs/project/BENCHMARK_VERSIONING.md).
  Advance case and oracle revisions independently; execution settings stay in
  the sealed suite. Preserve historical aliases and results. Resealing shared
  grader bytes alone is not a semantic revision bump.

- **`obench/run.py` `ROW_FIELDS` / append / resume** — corrupt JSONL or dropped
  fields silently skew resume and published claims; keep append fsync + fail-closed
  corrupt-line handling.
- **Suite config/plan/result seals** — final Harbor config bytes, plan digest,
  suite manifest, task-set identity, and atomic result/run-manifest pair must
  remain mutually bound. Never write partial suite results.
- **`exec_mode` / docker fallback** — never mix docker and local cells in one
  comparable results file (`docker_fallback` defaults off).
- **Publish digests** — `task_content_digest` must cover oracle inputs
  (`checker_data/`); verify must FAIL on missing digests.
- **`obench/report.py` aggregates** — key by `(harness, model)`, not harness alone.
- **Auth / proxy / transcripts** — auth is read-only staging; transcripts are
  LOCAL-ONLY and never published unscrubbed (`obench/scrub.py`).

## Git

- Merge strategy: merge commits (`gh pr merge --merge`), matching `main`'s
  history. Branches are not deleted on merge.
- Merging to `main` deploys nothing and cuts no release: this fork has no Pages
  site, deployment, or release automation. Publishing results is an explicit
  `obench publish` / `obench site build` step.
- `origin` is upstream and the GitHub parent of this fork, so `gh pr create`
  defaults to the wrong repository; always pass `--repo davisbuilds/openbench`.

## Working Agreement

- **Clarify before building** when a request is vague, contradictory, or skips a
  key decision; clear, well-scoped requests need no interview.
- **Fix small issues now; backlog the rest.** Fix simple, quick, or blocking
  issues in this repo as part of the current change and call them out. Record in
  `docs/project/BACKLOG.md` only what can't be finished now or needs the
  maintainer's decision; don't open a branch or PR just for a backlog entry.
- **Keep docs current** when a change makes a documented contract, boundary,
  procedure, or direction stale.
- **Planning and continuity.** Reuse clear task context. Save planning docs when
  decisions, acceptance, or coordination need persistence. Leave a brief handoff
  only when needed to resume unfinished work. Commit shared docs and keep status
  current. Before archiving, preserve lasting decisions and open work in
  maintained docs and repair links. Keep public docs self-contained and private
  evaluation context with its owner.
- **Commit and push.** Commit coherent chunks, staging by explicit path. Push
  your task branch to `fork` and open a PR against `davisbuilds/openbench` once
  the work is complete and the local gate passes; don't push directly to
  `main`. Opening an issue or PR upstream publishes to a third party: only when
  asked.
- **Merge** only under the standing merge authority in the maintainer's
  workspace instructions, or when the maintainer asks. Without those
  instructions in context, leave the PR for the maintainer.
- **Re-ground after compaction** in source, this file, the owning docs, and
  recent commits. A summary and your own earlier proposals are not user approval.
