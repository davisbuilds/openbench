---
status: implemented
---

# Repair validation before further model campaigns

## Outcome

An operator can inspect the repair contract, replay a frozen submission, and
validate task quality without running a model or reconstructing grader internals.
Passing runtime qualification alone must not imply task validity. Existing
results and historical oracle semantics remain unchanged.

## Sequence

1. Add a separately versioned Dojo oracle with representative plugin layouts,
   named checks, and source-identity boundary controls. Retain old versions.
2. Add `obench repair inspect`, `replay`, and `validate`: structured evidence,
   explicit scope and failures, real isolated execution, baseline rejection,
   distinct valid alternatives, and targeted defective-repair controls. Bind
   receipts to the task, source, implementation, and immutable runtime image.
3. Require current quality receipts alongside runtime admission at repair campaign
   launch and execution. Missing, failed, stale, or partial evidence blocks launch.
4. Supply writable project-local dependency scaffolding, pinned CSS tooling, and
   Bash as the solver's login shell. Exercise build and test workflows offline
   through the actual harness; report exact project commands and task identity.
5. Run regression, isolated-worker and actual-harness offline controls; update
   operator docs and delivery state. No authenticated qualification or scored
   benchmark runs during this pass.

## Evidence and limits

Regression tests must distinguish invalid fixtures, missed source replacements,
duplicate controls, invalid submissions, stale receipts, and worker failures.
Correct references and targeted partial repairs run through the production
grader. A passing control suite establishes the declared coverage, not universal
task validity or calibrated difficulty. Human contract review and independent
future holdouts remain necessary. Hidden inputs/observations stay operator-side;
the solver image contains no quality gate, oracle, or reference repair.

The broader task-pool and repeated-model calibration effort follows this pass.

## Implementation and verification

Implemented the new oracle, operator CLI, campaign admission requirement and
runtime v3 tooling. Private replay controls cover both current task families:
untouched baselines, two different accepted repairs, and targeted defects in each
scoring category. The Dojo controls distinguish two real source-identity bugs
that earlier fixtures missed. Historical scores have not been rewritten.

Actual pinned Codex with a fake provider now exercises the project workflows.
The AgentMonitor pre-fix checkout completes typecheck, lint, frontend build and
its public test suite (804 pass, one skip). Dojo completes its public profile
probe tests. These are workflow controls, not model attempts or full Dojo test
coverage. A deliberate failing command is rejected and cleanup is confirmed.

The longer workflow exposed two diagnostic defects, fixed in this pass:

- An AND-list failure could be hidden by a later successful marker command.
  Execute project checks in a separate fail-fast Bash invocation.
- A yielded tool call could trigger the fake provider's final response before
  work finished. Wait explicitly for the running cell and require an actual
  completed command event. Runtime admission accepts the bounded wait requests.

Offline evidence stays under ignored `results/repair-quality-20261007/`.
Implementation is tracked in [PR #26](https://github.com/davisbuilds/openbench/pull/26).
Delivery requires green repository checks and Codex PR review. Model
campaigns remain on hold; new runtime authentication/boundary qualification and
separately reviewed difficulty calibration follow this pass.
