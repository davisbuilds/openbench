# PR-derived eval candidates

Status: candidate preparation, 2026-09-15. These are behaviorally validated
repair tasks, **not yet a calibrated golden set**. The target is repeatable
frontier non-saturation; separating Terra from Luna is useful but not required.

## Fork baseline

Initial reconciliation pushed `main` and `fork/main` to
`ee09e68a49a9b741ca2ac4bdeab524cc22567025`. The fork already contains upstream
`main` through `db193457a3d9128cd4d01fd839c2a890c186c9ac`; no upstream-main merge
was needed. The two existing local commits were pushed first, followed by the
adaptation of [upstream PR 50](https://github.com/minghinmatthewlam/openbench/pull/50).
It preserves explicit cache-write zero, legacy aliases and fork HOME isolation,
and applies missing-field handling to the Terra xhigh alias.

The adapted fix passed 1,680 offline tests on Python 3.13.3 and all four task
validator lanes. A separate parent review reran the 29 focused adapter tests.
[GitHub CI](https://github.com/davisbuilds/openbench/actions/runs/34976202829)
also passed on Python 3.11 and 3.13 for that exact main commit.

[Upstream PR 48](https://github.com/minghinmatthewlam/openbench/pull/48) remains a
separate integration: its model registry schema/discovery differs from the
fork's existing loader. PRs 43 and 47 remain open upstream, with their patches
already present locally. Do not reapply them or rebase the fork wholesale.

## Upstream refinements

Both existing PRs were refined after reproducing additional gaps:

- [PR 43](https://github.com/minghinmatthewlam/openbench/pull/43),
  `f90b52e`: require explicit workspace/telemetry observations before inferring
  no work; short substantive answers remain capability failures.
- [PR 47](https://github.com/minghinmatthewlam/openbench/pull/47),
  `7af268d`: enforce cumulative wall caps before a resumed retry can sleep or
  launch. The fork port also covers its serial and parallel queue paths.
- [Issue 51](https://github.com/minghinmatthewlam/openbench/issues/51) proposes
  the standalone HOME patch on `fix/codex-home-isolation-upstream`, commit
  `563c355`. It changes only the generic adapter and its offline tests; a PR
  awaits maintainer interest under the fork
  [issue-first workflow](FORK_WORKFLOW.md#the-flow).

The upstream PR branches contain no personal eval tasks or private source.

## Candidate set

| Task | Behavioral targets | Initial difficulty hypothesis |
|---|---|---|
| `am-benchmark-pr106` | Study identity/replay, legacy migrations, missing/unscored task×trial coverage | Hard: interacting persistence and aggregation contracts |
| `engram-batch-pr1` | Ordered batch content preservation, fail-closed malformed-store handling | Medium: sequencing plus no-mutation boundary |
| `dojo-evidence-pr60` | Authoritative context, invocation-surface gating, qualified identity multiplicity | Medium: provenance and cross-function policy enforcement |

A fourth, private, cross-language Git-diff case lives under ignored
`results/private-pr-tasks/`. Its source and oracle must not enter this public
fork. The broader easy/medium/hard probe and private-source details remain in
`results/pr-fix-probe-2026-09-14/report.md`.

Public case provenance records exact source commits and deliberate defect
reintroductions. Model workspaces omit history, reviewers' explanations, hidden
tests and solutions. Checks use real disposable Git repositories/SQLite/files,
exact test collection checks, positive controls, and regression guards. A
partial repair earns credit only for complete behavioral buckets. Historical
intermediate fixes are useful mutation controls, not proof of model difficulty.

These snapshots currently use the fork-local compatibility task contract under
`tasks-local/`. They are not yet Harbor task images or an admitted/public suite.
Canonical new suites use `obench run`; provision the matching Harbor environment
before presenting these as portable harness comparisons. Existing native
matrix experiments are a separate local calibration treatment.

## Live Codex smoke

On the **MacBook**, Codex CLI **0.154.0**, adapter commit `ee09e68`, both
`gpt-5.6-terra-xhigh` and `gpt-5.6-luna-max` successfully authenticated through
`codex.run` and wrote the exact requested marker file. No benchmark process was
active before launch. Execution was serial using existing subscription auth;
no credentials or raw transcripts are included here.

The smoke planted an inert skill canary under a disposable parent HOME. A real
`codex debug prompt-input` positive control included it; the same diagnostic
with the adapter's isolated HOME omitted it. Actual `codex exec` session records
were inspected before temporary cleanup: both contained developer context and
skill blocks, neither contained the canary, and their recorded models were
`gpt-5.6-terra` / `gpt-5.6-luna` with originator `codex_exec`.

Both actual usage events reported cache-write zero and normalized to
`vendor_split`. This validates the installed binary's home-discovery boundary
and authenticated adapter path. It does not establish complete isolation from
project/system resources or prevent broad filesystem reads in local mode.
Raw local smoke code and sanitized results:
`results/codex-live-smoke-2026-09-15/{smoke.py,summary.json}`.

The public native screening spec is
`experiments/specs/pr-derived-screen.toml` (three tasks, two arms, three trials).
It is prepared for later calibration and has not been executed.

## Calibration admission

1. Require buggy failure, reference success, independently repaired buckets,
   regression mutations and no false success on missing/empty test collection.
   Missing host dependencies are exit 77 with no score, never a capability miss.
2. Freeze task/checker/source digests and effective runtime versions. Record a
   fresh study identity and adapter commit. Treat pre-HOME-fix runs separately.
3. Run the established Terra xhigh and Luna max arms serially on the same host
   and treatment, initially three trials each. Preserve per-bucket results,
   strict success, partial score and excluded/infrastructure outcomes separately.
4. Investigate failures against the stated behavior and reference solution;
   exclude ambiguous prompts, oracle errors and infrastructure failures.
5. Repeat promising tasks on fresh trials before admission. A single failure is
   a calibration lead; a clean 1.0 is evidence to revise or replace the task.
   Do not tune on every run then report those same runs as held-out evidence.

The MacBook is an authorized execution host for this work. Use the Mini only
when a specific runtime or scheduling need justifies provisioning it. Neither a
model campaign nor Harbor provisioning is implied by the two small smoke calls.
