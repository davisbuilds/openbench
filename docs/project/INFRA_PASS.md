---
date: 2026-09-25
author: Codex
topic: pre-benchmark-infrastructure
stage: plan
status: complete
risk_profile: high
readiness: ready
source: User-authorized infrastructure pass following PR 13
---

# Pre-benchmark infrastructure pass

## Goal and authority

Make repeated isolated repair campaigns straightforward to launch, inspect and
compare, then support a second real repair family. The user authorized the full
implementation/review/merge/sync sequence. Ask only when a material unresolved
decision changes the contract. No scored campaign is implied by this pass.

## Sequence and acceptance

- [x] **1. Reporting integrity.** Reject malformed JSONL with a source line;
  preserve legacy retry semantics within their known identity; validate canonical
  Harbor/suite identities before aggregation. Never silently pool incompatible
  treatments. Use embedded plans for coverage where available. Proof: regression
  tests for corruption, mixed treatments, valid comparisons, missing cells and
  retries, plus existing reporter/suite tests.
- [x] **2. Persistent campaign operations.** A thin local launch/status interface
  wraps the canonical runner with tmux and macOS idle-sleep prevention, immutable
  launch receipts, duplicate-launch refusal and explicit completion/unknown
  states. Read Harbor progress and evidence; do not introduce another scheduler.
  Proof: real subprocess/tmux launch, interrupted launch and duplicate controls,
  fake-provider Harbor integration, no surviving sandbox resources.
- [x] **3. Runtime admission.** Gather existing offline controls and a bounded
  authenticated control into evidence bound to the actual image/platform,
  loaded implementation and selected model/effort. Enforce that record at
  campaign launch only after control and inspection paths exist. Controls can
  run before admission; missing/stale evidence cannot authorize a campaign.
  Proof: accepted matching evidence and rejected changed/missing evidence before
  credentials or model calls; effective-runtime controls on the execution host.
- [x] **4. Trusted oracle boundary and second task.** Separate generic source
  extraction/worker lifecycle from a trusted oracle registry. Preserve historical
  digest verification. New tasks bind their own oracle/protocol and the shared
  boundary with explicit versions. Prove this using corrected AgentMonitor PR #106, including its alternative valid repairs.
  User selected #106 on 2026-09-25; #123 remains separate extra-hard oracle work. Do not weaken the
  authority boundary or make solver code a host plugin.

Each slice gets relevant tests, a fork PR, a completed Codex review, addressed
threads and green CI before merge. Sync the laptop and inactive clean Mini
checkout; never move a checkout while its benchmark is running.

## Grounding and constraints

- Baseline: merged PR 13, `7206d33`; source synced on both hosts.
- `results_query.load` skips bad JSON; `_arm_cells` ignores frozen treatment
  identity. `stats.py` already validates canonical comparison/suite identities:
  reuse it instead of defining another model.
- `suite_run.run_suite` verifies loaded runtime modules but does not consume an
  admission record. Existing control scripts are under `scripts/local/` and
  `scripts/ci/`; CI's fake provider does not prove authentication.
- `compile_suite` and `sandbox_grading` hard-code Dojo. Scheme 3 hashes the whole
  grading module, so historical verification needs an explicit compatibility
  design before extraction. Stage 4 contract is recorded in TRUSTED_ORACLE_BOUNDARY.md.
- Harbor owns scheduling, retries, locks, trial lifecycle and resume. Raw
  transcripts and private configuration remain local-only under `results/`.
- Changes belong on the development checkout. Unattended controls belong on the
  execution host, at an exact pushed commit. No credential copying.
- Defer dashboards, new storage services, parallel scheduling and bulk legacy
  removal. Existing aggregate-volume-quota backlog remains open.

## Authority, evidence and recovery

Trusted operator code may launch Harbor and read local receipts; solver code may
only access the established confined workspace and restricted model route.
Candidate bytes never select executable host plugins. Admission evidence is
produced by trusted controls, validated against actual inputs before dispatch,
and revalidated at execution; a claimed hash alone is not proof of a control.

Launch intent must precede process creation. A crash without a completion receipt
is unknown/interrupted, never success. Check actual process and Harbor state
before a new launch; resume through Harbor's existing identity checks. A status
query cannot mutate results or infer a successful trial from mere process exit.
Old runs and oracle versions remain unchanged; changed treatments get new IDs.

## Progress / continuation

- Durable priorities: `BACKLOG.md`, local commit `497303d`.
- Step 1 merged in PR #14 (`4ec77a7`); all CI checks pass and both review
  findings are resolved. Both checkouts synced. See `docs/results-queries.md`.
- Steps 2–3 merged in PR #15 (`0f28026`), synced on both main checkouts.
  Two cloud review passes, all four findings resolved, all CI green. Final Mini
  qualification at `803d58d` passed all offline/authenticated controls; admitted
  exact resume preserved evidence and made no additional model requests.
- Step 4 merged in [PR #16](https://github.com/davisbuilds/openbench/pull/16)
  (`241a3e2`). Final implementation `7fac584` passed all CI checks and cloud
  review with no findings. All nine Mini offline controls passed, including
  ten behavioral variants and five registered Harbor lifecycle controls.
- Both authenticated file-edit controls passed. Admitted exact resume
  revalidated the sealed two-row suite with four model requests before and
  after; qualification evidence stayed valid and sandbox cleanup completed.
  The execution host's `results/infra-pass/am106-qualification-launch-v3.json`
  and `am106-resume-proof.json` locate the local evidence. Earlier authentication
  and misplaced-edit failures remain separate attempts. A managed-token refresh
  restored auth; clarified append instructions retained strict byte checking.
  The observed CI canary-log race has a reproduced regression and verified fix.

## Next phase

The infrastructure pass is complete. Develop AgentMonitor #123 separately using
[EXTRA_HARD_REPAIRS.md](EXTRA_HARD_REPAIRS.md): neutral concurrency instrumentation,
complete state invariants, and alternative valid repair controls precede task
packaging. Runtime admission and corrected #106 do not establish extra-hard
benchmark difficulty. No scored campaign was launched by this pass.
