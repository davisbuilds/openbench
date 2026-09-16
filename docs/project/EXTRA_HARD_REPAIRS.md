# Extra-hard PR repair tier

Status: implementation and admission work in progress, 2026-09-16.
Contract: [extra-hard repairs](../specs/2026-09-16-extra-hard-repairs-spec.md).
No case is labeled extra-hard yet.

## What should make these tasks hard

The useful unit is a **repair trap**: a plausible local fix restores one
behavior while breaking another advertised invariant. Each case should require
building a correct model of the state transition, rather than remembering a
special filename or making one regex larger.

Use real repair history to find those traps, then construct small adversarial
scenarios that make them observable. Preserve enough source and ordinary tests
for diagnosis; give complete behavioral requirements without identifying the
faulty line or prescribing the reference algorithm. Hardness should remain when
an alternative correct repair is accepted.

Prefer a few different mechanisms over dozens of variations of one mechanism:

| Family | Plausible repair that should still fail | Required preservation |
|---|---|---|
| Concurrent schema/data upgrade | Lock writes after inspecting old state | Both workers recheck under ownership; data conversion occurs once |
| Concurrent read availability | Put every startup/read behind a write lock | Already-current readers continue under a WAL writer |
| Ordered batch mutation | Validate every operation against the starting snapshot | Later operations see earlier results; invalid batches preserve unrelated content |
| Qualified identity | Collapse by display name or normalize every path the same way | Equivalent entries compare equal; distinct origins and multiplicity remain distinct |
| Partial failure/recovery | Restore whichever files are easy and continue | Defined failure outcome, preserved data and safe retry; never assume historical rollback was complete |

The last row is a reserve until its full reference behavior is independently
proven. A merged fix is not automatically a complete oracle.

## What the first screen actually taught us

The native screen is now diagnostic only. Two Dojo Luna full-score attempts
read the hidden verifier and reference solution from a sibling worktree. Several
AgentMonitor failures were caused by unstated ID formatting or a test invoking
an internal migration function instead of the advertised startup path. More
complicated tasks on that same execution route would produce more complicated
measurement errors.

Keep those original rows and task versions unchanged. Correct tasks get new
versions and new campaign identities. Do not turn observed target-oracle
non-reads in other transcripts into an isolation claim.

## Candidate order and current evidence

1. **AgentMonitor PR123: concurrent upgrade.** Offline prototype forces two
   real SQLite processes into the relevant ordering. Broken source fails both
   the structural race and exactly-once data migration; the intermediate repair
   fixes only data conversion; the reference fixes both (three repetitions per
   source/bucket). A writer-held read-progress control rejects an overbroad
   serialization mutation. Remaining admission work: neutral instrumentation
   accepting another valid synchronization strategy, broader final-state guards,
   and portable dependencies. Not model-calibrated.
2. **Broader Engram PR1 batch family.** The earlier candidate starts after most
   batch-conflict and stale-validation repairs were already applied. A new case
   can start before that first repair and require evolving-state validation,
   destination ownership, current-state validation and content preservation
   together. Kernel lock/crash semantics are a distinct contract, not free
   extra difficulty to claim from the existing curation tests. Not packaged yet.
3. **Corrected AgentMonitor PR106 and Dojo PR60.** These are fair repeat/control
   candidates. They are not automatically the extra-hard tier. Their alternative
   valid repairs must pass before interpreting any further model misses.
4. **Private transactional repair reserve.** Keep private provenance local.
   Promotion waits for full automatic failure-path and rollback proof; a helper
   unit test does not establish end-to-end atomicity.

Implemented admission artifacts:

- `tasks-local/am-benchmark-pr106-v2`: public startup migration fixtures and
  behavior-based identity/coverage assertions; baseline/reference 0/1, partial
  repairs, and four passing alternative implementations. Original v1 is unchanged.
- `harbor-tasks-local/dojo-evidence-pr60-v2`: source-only container image and
  post-agent verifier; baseline/reference 0/1, six partial repairs and a valid
  alternative diagnostic representation. Effective Docker canary controls pass;
  an offline Harbor reference trial also returns 1. Live/authenticated execution
  and production network policy remain unproven.

The task-image package is not yet bound through the stock canonical exporter;
its README describes the dependency-only base-image path needed before claiming
suite import/publication readiness. Do not call a successful direct Harbor
oracle trial a sealed OpenBench suite result.

Prototype scripts and source-pin receipts remain local under
`results/extra-hard-preparation/`. They are feasibility evidence, not frozen
public benchmark tasks.

## Clean calibration comes after admission

Use an enforced container/Harbor read boundary with source-only agent images,
post-agent grading, and a frozen retrieval policy. Verify actual direct and
indirect access refusal, including a deliberately exposed control that proves
our detector would catch a leak. Prove the live model transport still works
without permitting answer retrieval. Do not silently relax to public networking
when a runtime cannot enforce the selected policy.

Keep exact effective model, effort, CLI version and task/driver/environment
hashes. The current stock Harbor profile defaults are not the earlier native
Terra-xhigh/Luna-max treatment; do not relabel them or pool their observations.
Run from an exact pushed commit on an awake host in tmux with logs and an exit
receipt. The Mini is the preferred unattended host for this follow-up, once its
execution lane passes the same checks.

Screen three attempts per model; then independently confirm selected cases on
fresh attempts, per the contract. Keep an easy control on the same runtime.
Report strict artifact success, behavior-group scores, completion/timeout,
exclusions and individual failure mechanisms. A timeout with passing code is
not a missing event, and a convenient six-attempt subset is not confirmation.

## Immediate integration work before live repeats

The isolated runtime is available: Docker builds and the pinned Harbor 0.20.0
reference trial both ran successfully. The missing work is in the real model
path, not a missing container engine:

- Preserve native treatment identity explicitly. Stock Harbor currently pins
  Codex 0.144.5 and medium effort; it rejects the Terra-xhigh/Luna-max names.
  Add narrowly pinned stock treatments for Codex 0.154.0 with their requested
  effort and disabled provider-side web search, retaining existing defaults and
  the stock OAuth lifecycle. Test plan/config/result identity round trips.
- Verify actual installed CLI version against the requested treatment. Matching
  ATIF and result versions alone does not prove either matches the requested
  version. Fail closed on a mismatch before admitting results.
- Prove the selected model-transport route permits the required API traffic and
  denies answer retrieval and unintended host services. A hostname allowlist for
  a host-side counting proxy is not a port-specific grant; do not broadly expose
  host services as a shortcut. Use a controlled allowed/denied endpoint pair.
- Complete canonical task binding (dependency-only immutable base images) and
  port corrected AM106 to the same admitted lane. Confirm buggy/reference and
  alternate repairs there, then freeze an exact pushed run on the Mini in tmux.

No new model calibration was launched during the audit/admission repair slice.
The current local evidence distinguishes direct Docker probes, a Harbor oracle
run, and future authenticated model admission; they are not interchangeable.
