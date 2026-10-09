---
status: in-progress
---

# First browser evaluation

Design authority: [practical agent evaluation](../project/PRACTICAL_AGENT_EVAL_DESIGN.md).
Implement one activity explorer, not a broad task pool. The first task uses
static HTML/CSS/JavaScript and a supplied JSON data contract. Its app is served
without executing candidate Node/build configuration in the trusted evaluator.
Visual preference remains a separate human assessment.

## Work sequence

1. Pin browser runtime dependencies on the existing developer image; prove
   Chromium launch under confinement before selecting runtime policy.
2. Add an activity-explorer contract and runnable starter, independently graded
   functional and layout observations, and two structurally different valid
   controls plus targeted defective controls.
3. Wire task seals, canonical Harbor grading, artifact replay, and quality
   admission; preserve existing repair semantics and reseal shared-byte changes.
4. Exercise real browser behavior, negative controls, cleanup, artifact evidence,
   and actual Codex screenshot/tool transport. Run appropriate regression and CI
   checks, open the implementation PR, and request Codex review.
5. Qualify the reviewed runtime on the execution host. Keep comparative model
   pilots separate until all task and runtime gates pass.

## Acceptance and limits

- Long text, responsive layout, filters, selection, empty/error/retry behavior,
  and keyboard access have explicit public requirements and independent checks.
- Both accepted layouts pass; deliberate functional, clipping, navigation,
  nested-scroll and hidden-content defects fail the relevant checks.
- Solver and browser cannot access host files, credentials, acceptance code,
  reference controls, or verdict-writing authority. Runtime policy and actual
  enforcement must agree; no host network/IPC or privileged browser workaround.
- The real agent receives image observations, not just screenshot filenames.
- Receipts bind task, runtime, source, browser, inputs, outcomes, and artifact
  evidence. Failed and incomplete attempts remain visible.
- The initial layout checks establish their specified coverage, not general
  accessibility certification, design taste, or calibrated frontier difficulty.

## Implementation checkpoint

Implemented the task, pinned browser layer, closed browser-only seccomp profile,
host-owned observation predicates, screenshot transport, and separate browser
runtime admission. The normal repair-only execution profile is unchanged.

Local real-container checks accept both list/card controls and reject eleven
specific defects. Harbor accepts the valid submission, rejects a source symlink,
and overwrites a forged reward with the trusted zero. The actual Codex loop
renders a CSS change and delivers distinct before/after PNGs to the synthetic
provider unchanged. No live inference was used for these checks.

Chromium needs user-namespace syscalls plus `chroot` under Docker's dropped-cap
policy. It launches with its sandbox enabled; host network/IPC, elevated
capabilities and disabling the Chromium sandbox are not used. A candidate-free
Chromium launch precedes every grade so a broken runtime cannot become a model
failure. Worker receipts retain Chromium/Playwright versions and policy hash.

Cloud review identified two false-negative predicates: status/paragraph
presentation and metadata inside accessible buttons. Both have red/green
regressions and a strengthened card control. A further real-browser control
reproduced premature loading/filter sampling; bounded observable-state waits
now accept deferred initialization and debounced filtering. Missing visible
titles and truncated descriptions remain rejected.

Local validation passed 2,039 tests and fresh replay of fourteen browser
controls before the asynchronous-wait correction. Runtime qualification also
passed on the execution host, including all three model/effort controls and six
completed HTTP-200 streams. Those earlier receipts remain diagnostic evidence;
the correction requires renewed task seals, workflow/quality and runtime gates.
Hosted Docker CI remains a required delivery gate.
Keep screenshots/transcripts and machine-specific qualification paths local.
The first task is not yet a calibrated difficulty benchmark.
