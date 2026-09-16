---
date: 2026-09-16
author: codex
topic: extra-hard-repairs
stage: spec
status: in-progress
source: conversation
risk_profile: high
readiness: ready
---

# Extra-hard Repair Benchmark Spec

## Problem

The user needs coding-agent comparisons that reveal meaningful remaining
capability limits. The first PR-derived screen produced high scores on several
cases, but inspection found two agents reading hidden reference material and
several hidden assertions stricter than the advertised task. Neither apparent
success nor apparent failure is trustworthy evidence of difficulty under those
conditions. Harder prompts cannot repair that measurement problem.

## Contract

The benchmark exposes realistic repair problems whose challenge comes from
interacting invariants, with adversarial examples drawn from the same stated
contract. Every scored implementation is judged by observable behavior, not by
matching the reference implementation's internal choices. A case is admitted
only after behavioral polarity, plausible wrong-repair controls, valid alternative
repairs, and effective read isolation have all passed. `obench validate` remains
the polarity check for native-compatible candidates; container execution and
access-control probes must additionally prove the actual scored route.

An "extra-hard" label requires fresh isolated model evidence. Creating a task,
seeing an old implementation fail, or observing a single model failure is not
that evidence. No model-performance claim is made by this contract.

## Success Criteria

- SC-01: Each candidate has a traceable real repair family, an explicit user-visible
  contract, a known working reference, and a reproducible broken baseline.
  Public and private provenance remain separable.
- SC-02: Each challenge crosses at least two interacting concerns, such as
  transaction ownership and schema evolution, sequential validation and mutable
  state, or identity equivalence and origin multiplicity. Unrelated small bugs
  bundled into a long prompt do not satisfy this criterion.
- SC-03: The baseline fails the intended target behaviors; the reference passes
  all target and preservation checks; at least two plausible partial repairs
  fail different advertised obligations. At least one structurally different
  correct implementation passes. Concurrency/state cases require a genuinely
  different valid synchronization or control strategy, not merely renamed
  identifiers. Contract-equivalent representations are additional fairness controls.
- SC-04: Agent-visible material contains no grader, solution, fixing commit,
  earlier trial transcript, or unadvertised answer-bearing fixture. Effective
  execution denies access to known-present external answer canaries while
  allowing intended workspace operations. Failure to demonstrate the boundary
  prevents model dispatch for calibration.
- SC-05: Each observation binds task, oracle, environment, harness, model/effort,
  attempt identity, independent score, completion/timeout and evidence. Missing
  evidence, changed inputs and host interruptions remain explicit; retries never
  replace original observations silently.
- SC-06: Development and confirmation observations remain distinct. Difficulty
  is reported by family, graded score and strict full success. Repeated examples
  of one edge case do not masquerade as independent tasks.

## Evaluation

This is a measurable experiment bet: a mixed tier grounded in real PR fixes
should expose failures that survive fair contracts and isolated execution.

Start with three screening attempts per model per candidate, including an easy
control on the same runtime. Freeze tasks before dispatch. A candidate enters
extra-hard confirmation only if neither model fully solves more than one of its
three screening attempts, subject to audit of every failure. This is a selection
rule, not a confidence statement. Cases with higher success can remain medium
controls rather than being made artificially obscure.

Full solve means every mandatory target and preservation group passes, not a
rounded mean score. Freeze group weights and budgets before screening. Artifact
correctness and harness completion are separate: a timeout with a passing artifact
retains both facts. Report launched attempts and infrastructure exclusions.

Use six fresh attempts per model for a selected case before labeling it
extra-hard. Require at most one full solve per model in those six; retain graded
scores and uncertainty, including zero-full-success outcomes. These counts are
operational screening thresholds, not proof that the true success probability
is below a particular percentage. Do not launch the full confirmation allocation
without first reviewing screening evidence and expected runtime. The label is
scoped to the pinned models, harness settings and budget. If confirmation misses
the threshold, retain or downgrade the case; do not keep selecting favorable
six-attempt subsets or rerun until the label passes.

Reject or revise cases whose misses depend on unstated formatting, a brittle
private implementation seam, intermittent timing luck, oracle access, or an
unproven reference. Stop calibration on evidence or isolation failure. A task
revision starts a new version and does not inherit old performance claims.

## Scope

In scope: real multi-file repairs plus adversarial but realistic combinations,
concurrency and recovery, deterministic hidden behavioral checks, partial repair
measurement, and comparisons on explicitly pinned Terra/Luna settings initially.

Out of scope: trick wording, arbitrary algorithm puzzles unrelated to the repair,
memory of specific public patches as a target skill, changing budgets only to
force failure, model training, public leaderboard claims, or the separate private
skill-effect experiment. Larger datasets and additional model arms are later
choices, not prerequisites for the first valid case.

## Assumptions And Constraints

- Public repair provenance may already be known to models. We can prevent live
  answer retrieval but cannot certify training-data absence; report that limit.
- The isolated runtime is a new treatment. Do not pool native MacBook scores with
  Linux/container results or silently substitute reasoning effort or CLI version.
- Race verifiers must force an observable interleaving, and accept alternative
  valid synchronization strategies. A short sleep is not a synchronization proof.
- A shipped historical fix may itself be incomplete. It becomes a reference only
  after proving the exact task contract, not because a PR merged.
- Full-score failures may be useful; inflated partial-score granularity is not.
  Weight meaningful behavior groups, and separately report which obligation failed.

## Authority And Safety

The coding agent may inspect the advertised source and modify its disposable
workspace. It may not inspect grading/reference assets, other attempts or host
repositories. Local filesystem isolation and retrieval policy must be verified
through the actual execution route, including indirect access. Freeze the
retrieval policy per treatment for shell HTTP, Git/package downloads, browser/MCP
tools and harness-native search; permit required model/auth transport separately.
Prove blocked retrieval using a known-present controlled endpoint, not an accidental
network outage. No live benchmark-answer retrieval is permitted. Grading consumes
only the permitted artifact after agent execution stops. No grader or result
claim authored by the candidate is authoritative.

The operator owns authenticated lanes and frozen input identity. Existing auth
may be used only within already-authorized host/lane scope; no cross-host daily
credential copying is implied. Unknown execution or network policy is an
unproven prerequisite. Original attempts and evidence survive interruptions.

## Evaluation Scenarios

- EV-NEG-01: Known-present external answer canaries cannot be read directly,
  through symlinks, or through subprocesses; intended source reads and writes
  succeed in the same effective environment. Answer retrieval outside the
  advertised material is refused by the runtime policy.
- EV-REC-01: A terminated attempt retains its identity, completion state and
  independently graded artifact if available; absent evidence never becomes
  a clean solve. A new attempt cannot overwrite the original.
- EV-CON-01: Two workers upgrading or applying against shared state preserve
  the stated invariant under a forced interleaving. A valid serialized repair
  and an alternative valid strategy both pass; partial repairs do not.
- EV-LEG-01: A truly pre-upgrade fixture reaches the advertised public startup
  or repair path with data preserved. Equivalent identifiers are accepted when
  the contract specifies equivalence rather than an exact rendering.

## Readiness Review

- Deterministic validation: passed
- Adversarial critique: complete
- Closure critique: complete
- Blocking findings: none
- Unproven execution prerequisites: actual harness read/retrieval boundary, exact
  runtime/effort integration, and alternative-repair controls for each admitted case

## Open Questions

None about the intended contract. Runtime admission is an execution prerequisite,
not permission to weaken read isolation or silently change the comparison arms.

## Handoff

1. Repair the measurement boundary and version unfair existing tasks.
2. Admit the strongest interacting-state prototype after alternative-repair controls.
3. Bind exact runtime/effort checks and receipts before model dispatch.

Current work does not claim an admitted tier.
