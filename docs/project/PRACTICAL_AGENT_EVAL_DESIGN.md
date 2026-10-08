---
date: 2026-10-08
status: proposed
readiness: draft
risk_profile: high
---

# Practical agent evaluation design

## Purpose and decision status

Measure how reliably an agent can take useful engineering work from intent to a
verified outcome, and how much human supervision that requires. PR-derived
repairs remain valuable, but do not represent the full working relationship:
agents also build features, diagnose symptoms, verify work, recover interrupted
operations, and make product decisions.

The owner agreed to broaden evaluation beyond repairs and explicitly included
product implementation and usability. The initial direction comprises four
families: evolving feature development, verification and diagnosis, operational
recovery, and product implementation and usability. The measures and sequence
below are recommendations for implementing that direction. Individual task
contracts, study size, scoring thresholds, and execution changes are not yet
frozen. This document does not authorize or announce a benchmark launch.

The intended output is a capability profile useful for choosing a model,
harness, context bundle, and level of delegation for a particular job. Avoid
collapsing everything into a single leaderboard score.

## Principles

- Ground tasks in representative work, with enough checkout, tooling, data, and
  context to support the workflow a developer would reasonably use.
- Difficulty should come from interacting requirements, investigation, evolving
  state, and consequential choices. Missing tools, misleading fixtures, and
  unstated requirements are evaluation defects.
- Establish runtime readiness, task validity, and model performance separately.
  A passing runtime control does not prove a task is valid or difficult.
- Prefer observable behavior and final state over resemblance to a reference
  implementation, agent self-reports, line counts, or prescribed tool sequences.
- Retain solved controls and ordinary cases alongside difficult ones. Selecting
  only tasks that current models fail distorts practical reliability.
- Keep executable correctness, human preference, efficiency, and operational
  failures separately inspectable.

## Initial evaluation families

### 1. Evolving feature development

Start with a bounded feature in a realistic project, then introduce successive
requirements after each submission. The agent extends its own prior work. For
example: implement an activity view, add filtering and pagination, then support
a changed data contract while preserving compatibility.

Measure acceptance at each checkpoint, retention of earlier requirements, later
change effort, regressions, and human correction. Preserve intermediate source
and results. A final success must not erase failed intermediate checkpoints.

Freeze the sequence before comparative trials. Each stage must state its own
requirements; agents are not penalized for failing to predict undisclosed future
features. Architectural quality is primarily observed through subsequent work,
not inferred from a preferred pattern or smaller diff. For diagnosis, separate
end-to-end runs from follow-up tasks starting at a common accepted checkpoint;
do not pool those treatments.

### 2. Verification and diagnosis

Use two related task forms:

- **Diagnose symptoms:** supply a working environment with an observed problem
  and several plausible explanations. Require a supported cause, discriminating
  reproduction, and an appropriate correction or next diagnostic step.
- **Assess completed work:** supply a purportedly finished change and its
  evidence. Cases can include a stale built artifact, missed consumer, misleading
  test, or genuinely correct implementation.

Measure material defect detection, false alarms, reproduction quality, correctness
of causal claims, and unsupported completion claims. A useful probe distinguishes
competing explanations; a long command history is not success. Include clean
cases and insufficient-evidence cases. The agent should distinguish correct,
incorrect, and unresolved, with uncertainty tied to a specific missing fact.

Freeze the known defect inventory and adjudication rules before screening. Allow
review of novel valid findings rather than automatically labeling every finding
outside the inventory false. Keep adjudicated changes separate from original
scores and apply them consistently across submissions.

### 3. Operational recovery

Use disposable services and data to model interrupted imports, partial
migrations, duplicate delivery, conflicting state, and resumed background jobs.
Ask the agent to restore the requested outcome under explicit authority and
data-preservation constraints.

Measure externally observed final state, preservation of unrelated records,
idempotency, recovery after another interruption, cleanup, and scope compliance.
Reissuing an operation must not silently duplicate effects. Reporting success
requires checking the application state, not merely a successful command exit.

Simulate services and credentials; never use production data or accounts. The
task must make allowed actions, destructive-action boundaries, and recovery
expectations clear. An expected denial is only evidence if a known-positive
control establishes that the detector can observe the relevant operation.

### 4. Product implementation and usability

Evaluate three distinct dimensions:

| Dimension | Representative concerns | Evidence |
| --- | --- | --- |
| Functional correctness | Navigation, filters, forms, persistence, loading/error/recovery states | Browser interactions and independently observed application state |
| Layout and usability | Wrapping, clipping, responsive layout, nested scrolling, focus, keyboard access, zoom | Browser assertions, interaction traces, and rendered views across declared content/viewport conditions |
| Design quality | Hierarchy, typography, density, coherence, restraint, fit to the brief | Blinded human comparisons with anchored criteria and recorded rationale |

Use three task shapes: build from a product brief; improve a functional but
awkward interface; and revise a submission in response to design feedback.
The last form also exercises evolving development. Include both briefs with
open design choices and briefs with explicit visual references/preferences;
these measure default taste and ability to follow direction separately.

Supply realistic content: long labels, dense records, empty and error states,
and mobile layouts. Hidden cases vary content and viewport within declared
requirements. Do not make obscure text or an undisclosed screen size the only
reason a submission fails.

Avoid universal rules such as no wrapping or no scrollbars. A data table may
legitimately scroll horizontally; a nested panel that traps essential navigation
is a different problem. Check reachability, readability, intended containment,
and user journeys. Overflow heuristics alone are not a complete usability grader.

For taste, hide provider/model labels, randomize comparison order, permit ties,
and evaluate the same content, viewports, and interactions. Record criterion
scores alongside overall preference. A single owner's ratings measure that
owner's preferences, not universal design quality. Automated visual judges can
assist after calibration; they are not initially authoritative for taste.
Pixel similarity is appropriate only when fidelity to a supplied reference is
part of the task. It is not a general design-quality metric.

## Measures and success quality

Report the following separately, by family, task, and treatment:

| Measure | Operational definition |
| --- | --- |
| Complete outcome | All mandatory behavioral requirements pass in independent evaluation; required preservation/authority constraints also hold |
| Partial outcome | Named requirement/invariant outcomes, including explicit not-evaluated states; weights and mandatory vetoes declared before trials |
| Reliability | Complete outcomes across repeated independent attempts, with task-level results and uncertainty; one success does not establish consistency |
| Human burden | Necessary interventions, corrective turns, active review time, and rework; distinguish scripted assistance from measured human attention |
| Efficiency | End-to-end and stage time, measured token/usage data, and cost where actually available; include failed-attempt spend in cost per successful outcome |
| Verification quality | Reproducible evidence, detection/false-alarm rates on labeled cases, and unsupported completion claims |
| Operational integrity | Timeouts, provider/transport failures, invalid or incomplete evidence, unwanted side effects, and authority violations |
| Product preference | Blinded preference/ties and rubric dimensions, reported separately from functional and usability outcomes |

Every planned attempt retains a record. Report both overall operational success
and task performance conditional on valid execution, with explicit denominators
and exclusions. Infrastructure failures are not ordinary wrong answers and must
not disappear from reliability or cost reporting. Missing usage is unknown,
not zero; subscription usage must not be represented as measured dollar billing.
When no attempt succeeds, cost per successful outcome is undefined.

Record answer/artifact correctness separately from the agent's completion claim.
A correct partial artifact can coexist with a timeout; a polished explanation
can coexist with an incorrect result. An honest unresolved diagnosis should be
distinguished from both confident error and verified success, according to the
task's predeclared acceptance criteria.

For review tasks, report precision/recall against the adjudicated defect set and
false-positive rates on clean cases. For abstention, report coverage and accuracy
on answered cases together so refusing everything cannot appear strong. For
layout detectors, prove detection with representative defective and acceptable
interfaces; suppressing all overflow or hiding content must not satisfy the test.

Success for this program means the measurements support practical decisions
about delegation and setup. It does not require every task to separate models.
Before making a ranking claim, freeze the decision-relevant effect size and
choose sufficient tasks/attempts for that comparison. Report uncertainty and
task-family variation; checkpoints from one task are not independent samples.

## Experimental treatments and task quality

Context/skills and continuity are dimensions across families, not separate task
types. Compare baseline guidance, a frozen workspace bundle, and selected skills
using the same task contracts and budgets. Measure outcomes and overhead, not
skill mentions or file opens. Freeze preference references as part of context;
keep calibration examples separate from held-out taste tasks.

Continuity treatments can introduce a context reset, handoff, or user correction.
Measure lost requirements, repeated work, conflicting edits, and recovery.
Begin with scripted user responses and explicit intervention rules for repeatable
interaction; actual human burden needs a separately measured human-in-the-loop
sample. These are not interchangeable observations.

Pin model, effort, harness version, effective instructions, tools, context,
runtime, task revision, time/request budgets, and user interaction protocol.
Native harness prompts/tool behavior may differ by model: report that as part
of the treatment rather than calling it a model-only comparison. Randomize or
interleave model order and freeze the schedule and retry rules before running.

Review contracts and graders independently of candidate outputs. Validate known
good alternatives, untouched or defective cases, and targeted partial solutions.
For stateful tasks, include interruption/recovery controls. For UI tasks, verify
both interactions and representative visual failures. Preserve public development
tests while withholding acceptance inputs and grader authority.

Separate development from held-out task families; variants of the same feature
or source PR are not independent holdouts. Changes motivated by observed model
failures create a new task/grader revision and require future confirmation.
Preserve original results and avoid pooling revised tasks silently.

## Browser-capable runtime and grading

Source inspection on 2026-10-08 found frontend build tools in the repair runtime
but no provisioned Playwright/Chromium. Existing qualification does not establish
browser readiness. Build on the current isolation and evidence contracts; keep
the browser extension separately identifiable and immutable.

The proposed frontend runtime supplies:

- Pinned Playwright and matching Chromium, OS libraries, local fonts, icons,
  and task-appropriate package dependencies, available without runtime downloads.
- A project checkout with the configuration, assets, seed data, and documented
  commands needed for normal build, development, and browser tests.
- Non-root execution, bounded browser scratch/shared memory and process resources,
  and app/browser communication inside the isolated environment.
- An accessible path for starting/stopping the app, navigating, interacting,
  reading browser diagnostics, taking screenshots, and preserving traces.
- Image observation through each actual tested harness. Producing a PNG without
  delivering it to the model is not an admitted visual-feedback workflow.

Do not silently enable host networking, host IPC, privileged execution, daily
browser profiles, or credential access to make Chromium work. Browser launch
flags and sandbox behavior require explicit runtime evidence. A future research
lane needing internet access requires its own boundary and is outside this slice.

Admission must demonstrate, through the actual harness, that an agent can build
and start a representative app, inspect a screenshot, interact with it, edit a
visible defect, verify the changed rendering, and stop all descendants. Include
a controlled failed browser/test operation and host/network denial probes.
Use suitable frozen visual controls so source inspection alone cannot stand in
for evidence that the image observation path works.

The independent evaluator rebuilds the frozen submission and executes hidden
checks after solver processes stop. Candidate code runs with task-scoped
authority; acceptance code, expected results, credentials, and verdict-writing
authority remain outside its readable/writable environment. The browser/evaluator
topology and artifact allowances must be designed before implementation. Existing
repair source-subtree grading is not automatically sufficient for full frontend
applications or persistent service state.

Capture viewport, browser/image, fonts, locale/timezone, device scale, seed data,
and animation/time controls with rendered evidence. Apply the same capture
protocol to all arms. Preserve screenshots/traces privately and ensure cleanup
and retention account for their larger volume.

## Rollout and readiness gates

1. **Prove one browser workflow.** Add the frontend runtime and one representative
   UI task. Demonstrate build, actual image inspection, interaction, revision,
   independent grading, negative controls, and cleanup before expanding the pool.
2. **Develop a small balanced pilot.** Include all four families plus existing
   repair controls. Freeze task contracts, evidence schemas, intervention rules,
   and grading rubrics; independently validate them before model screening.
3. **Run a bounded repeated pilot.** Use explicit study authorization and current
   admission evidence. Audit failures for task/tooling defects before interpreting
   them as capability differences. Keep diagnostic retries linked and separate.
4. **Freeze a comparative study.** Select task families and attempt counts based
   on pilot variance and the practical decision. Use held-out families and the
   preregistered analysis, then report capability profiles and limitations.

Harbor continues to own execution/scheduling; OpenBench owns intent, evidence,
admission, grading contracts, and comparisons. Avoid building a parallel runner.
The existing [campaign workflow](../benchmark-operations.md),
[repair quality gate](../repair-validation.md),
[versioning policy](BENCHMARK_VERSIONING.md), and
[frozen context contract](FROZEN_CONTEXT.md) remain authoritative for their
current scopes. New task families need explicit extensions rather than bypasses.

## Later candidates

| Family | Useful task | Primary evaluation concern |
| --- | --- | --- |
| Data analysis and measurement | Audit duplicated trials, missing telemetry, changed configurations, or misleading aggregates | Reproducibility, valid comparisons, and accurate uncertainty |
| Research and technical decisions | Evaluate adoption of a dependency/service under stated constraints | Source accuracy, technical checks, tradeoff coverage, and justified uncertainty |
| Prioritization and scoping | Recommend a bounded improvement shortlist from a frozen repository/backlog | Validated relevance, human acceptance, effort calibration, and review burden |

These remain candidate families, not commitments to expand the first pilot.

## Decisions to resolve before dependent implementation or trials

- First UI project/framework, fixture content, and required user journeys.
- Browser launch isolation, resources, screenshot observation path, and independent
  grader topology, proved on the intended execution host.
- Full-application submission boundary and task-specific service/dependency setup.
- Viewport/content matrix, usability acceptance thresholds, preference rubric,
  raters, and calibration/held-out visual references.
- Treatment lineup, budgets, sample size, intervention protocol, and practical
  effect size for the first comparative study.

These questions do not block recording the direction. They do block claiming
the new families are ready to measure or that a browser package installation
alone qualifies the environment.

## Design precedents

- [SlopCodeBench](https://arxiv.org/abs/2603.24755): evolving specifications over
  an agent's own prior implementation; inspiration for observing later change
  consequences rather than awarding an architectural style score.
- [Terminal-Bench](https://www.tbench.ai/news/announcement): executable tasks in
  realistic terminal environments; a precedent for operational task packaging.
- [tau2-bench](https://github.com/sierra-research/tau2-bench): explicit agent,
  user, tool, and environment interaction; a precedent for controlled assistance.
- [Playwright visual comparisons](https://playwright.dev/docs/test-snapshots)
  and [Docker guidance](https://github.com/microsoft/playwright/blob/main/docs/src/docker.md):
  rendering reproducibility, browser/dependency matching, and container operation.

These inform the design; their published scores do not validate this task pool.
Private briefs, personal preferences, captured context, reference assets,
transcripts, and dated execution evidence remain outside public source.
