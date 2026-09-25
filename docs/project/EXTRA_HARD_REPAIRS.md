# Extra-hard PR repair tier

Status: implementation and admission work in progress, 2026-09-16.
Contract: [extra-hard repairs](../specs/2026-09-16-extra-hard-repairs-spec.md).
No case is labeled extra-hard yet.

The first isolated repeated screen has finished and its sealed suites reverify.
Captured artifacts also reproduce their original grading in fresh workers.
Execution now defaults to 20 minutes with separately bounded sandbox sealing;
credential-free Harbor controls cover near-deadline completion and real timeouts.
The diagnostic-contract audit found that v3 required unspecified difference-array
cardinalities. Revision 4 accepts name-only and qualified diagnostics while
checking mismatch detection, entry totals and return shapes against paired
equality/difference cases. Confirmation remains on hold pending runtime admission
and fresh screening of this changed treatment. See the
[benchmark discrimination backlog](BACKLOG.md#benchmark-discrimination).
Retain original scores and timeout classifications; any revised task or runtime
starts a new treatment. Detailed trial evidence remains local-only.

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

The opt-in [repair sandbox](REPAIR_SANDBOX.md) keeps the solver at
`network=none`, using a per-trial Unix socket to a restricted model gateway.
It also separates candidate execution from oracle/reward authority. Offline controls now exercise the actual Harbor environment and verifier.
Authenticated transport and atomic canonical import passed a short, separately
sealed negative control on 2026-09-16. Longer diagnostics exposed log-export and
usage-conversion defects, now corrected. A fresh 600-second Luna timeout
preserved all required evidence and passed atomic import and manifest
verification. See the linked sandbox reference for the distinct outcomes;
campaign admission and difficulty calibration remain separate.

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

## Remaining admission before live repeats

The opt-in `repair-v1` suite lane now pins Codex 0.154.0, preserves explicit
Terra-xhigh/Luna-max identity, enforces a model-only gateway, and grades Dojo v3
outside candidate authority. Task scheme 3 binds the external oracle and task
inputs. Existing stock profiles remain unchanged. See the
[implementation and control commands](REPAIR_SANDBOX.md).

Remaining work:

- Screen Dojo v4 as a new treatment after offline and authenticated controls;
  preserve v3 results and use the original pinned commit for historical replay.

- Preserve the short, large-log, timeout, and authenticated longer-run controls;
  renew them when runtime/adapter bytes change. These do not establish
  equivalence with the native treatment.
- Port corrected AM106 to the same boundary with its own external oracle.
- Freeze an exact pushed commit and admitted runtime on the selected execution
  host, then launch calibration in tmux with logs and an exit receipt.

The authenticated runs are controls and diagnostics, not admitted calibration.
Earlier rejected suites remain intact: one lacked a trajectory and another
had inconsistent converted usage. The fresh Luna timeout sealed successfully
after correction, retaining its timeout and partial artifact score. The native
contaminated screen remains excluded; these diagnostics do not establish that
any candidate is extra hard for frontier models.
