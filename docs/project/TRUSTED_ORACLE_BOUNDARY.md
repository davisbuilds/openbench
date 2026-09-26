---
date: 2026-09-25
status: in-progress
stage: plan
risk_profile: high
readiness: ready
---

# Trusted repair oracle boundary

## Contract

Keep the existing Dojo scheme-3 grader byte-identical. Historical tasks, results,
and verifier replay retain their original meaning. New repair tasks use a
scheme-4 binding over the task tree, trusted registry/oracle implementation,
public worker protocol, and shared extraction/execution boundary. Task metadata
selects a built-in versioned oracle ID; it cannot name arbitrary host code.

Corrected AgentMonitor #106 is the infrastructure proof (user choice); #123 is
separate extra-hard task development. #106 preserves revision-2 behavioral
requirements and all four valid alternative repairs. Packaging changes create a
new task treatment and require fresh difficulty calibration later.

## Authority and execution

- Solver receives instruction and buggy workspace in the existing confined
  runtime; it cannot read solutions, oracle expectations, host files, SSH routes,
  or sibling attempts.
- Freeze only after solver stop and broker revocation. All archive entries still
  undergo path/type/size validation. A trusted source policy selects submitted
  code. New source helpers within that policy are permitted; candidate paths do
  not select executable host plugins.
- Public worker protocol sends inputs and returns observations. Candidate code
  executes only in disposable networkless workers, with no host mounts or
  credentials. Expected values, comparisons, and reward files remain trusted.
- Native Node/SQLite dependencies are installed at image build time from a lock
  file, never downloaded during a trial. Image identity and worker protocol are
  part of evidence. Runtime failures stay distinct from candidate failures.
- Imported evidence binds the selected oracle, frozen source, worker boundary,
  actual task identity, and sealed suite. Unknown or mismatched versions fail.

## Delivery and paired controls

1. Add registry, generic worker lifecycle, scheme-4 binding and fail-closed
   validation. Retain the legacy route unchanged. Test unknown IDs, changed
   oracle/protocol/task bytes, source escapes, malformed/overlimit outputs, and
   known-positive legacy digest replay.
2. Port #106's input scenarios and host-side behavioral comparisons; provision
   locked native dependencies and package the corrected snapshot. Unit controls
   exercise every expected comparison and its rejected counterpart.
3. Connect canonical suite compilation/import and runtime qualification. Test
   mixed identity refusals and valid sealed imports; no task-selected host imports.
4. Run Mini Docker controls at an exact pushed commit: untouched 0, historical
   reference 1, partial repairs with expected bucket scores, all four valid
   alternatives 1, source/read-boundary probes, malformed worker output, cleanup,
   and canonical Harbor lifecycle/import. No scored model campaign in this pass.
5. PR, cloud review, addressed findings, green CI, merge/sync; record evidence in
   INFRA_PASS.md. Admission proves execution and oracle validity, not difficulty.

## Implementation progress

- New registry, scheme-4 binding, confined worker lifecycle, and #106 observation
  protocol are implemented on `feat/trusted-repair-oracles`.
- Canonical suite compilation/import routes the selected built-in oracle and
  binds all implementation modules. Legacy scheme-3 bytes remain unchanged.
- Locked Node dependencies and native SQLite build passed on Mini ARM64 and
  hosted Linux. All ten behavioral variants and five Harbor lifecycle controls
  passed; both Python CI lanes and the sandbox lane are green at `94ec429`.
- PR #15's final campaign fixes are integrated. Two cloud reviews completed
  without findings. The selected-task authenticated qualification reached the
  provider but received HTTP 401 for every request; admission and exact-resume
  proof remain pending a valid execution-host login. See INFRA_PASS.md for the
  continuation gate. No difficulty or full runtime-admission claim.
