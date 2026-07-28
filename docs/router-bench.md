# Router Bench draft

Router Bench is a proposed benchmark for **native automatic-routing products**:
given the same coding task and harness configuration, which product selects
models and providers that produce the best checked result under real latency,
availability, token, and cost constraints?

This document is a specification draft, not an implemented runner or a claim
that Router Bench results exist. The illustrative
[`router-bench-native-draft.toml`](../obench/examples/router-bench-native-draft.toml)
is intentionally not accepted by an `obench` command.

## Boundary with Gateway Bench

Router Bench and [Gateway Bench](gateway-bench.md) answer different questions.

- **Gateway Bench** is a fixed-model, fixed-provider transport comparison. It
  disables automatic selection, retries, and fallbacks so the gateway route is
  the treatment.
- **Router Bench** requests each product's native auto-router and permits the
  product to select a model, provider, and documented fallback path. Those
  decisions are the treatment.
- Router Bench includes preregistered, non-routing fixed-model baselines. A
  gateway arm locked to a fixed model belongs in Gateway Bench, even when that
  vendor also offers an automatic router.

All arms use one declared coding harness and the same checker-scored task set.
The checker is the sole correctness judge; router metadata and model output
never determine whether a task is solved.

## Experiment contract

A public experiment must bind and digest:

- the harness version, task contents, checker contents, workspace source, and
  execution lane;
- native router identifiers and adapter capability snapshots;
- fixed-model baseline revisions and providers selected before the run;
- prompts, sampling controls, tool schema, context limits, call/output/time/USD
  budgets, cache policy, and client retry policy;
- a seeded matched schedule, UTC windows, repetitions, replacement limit, and
  frozen price snapshot;
- the route-evidence policy and every metric definition.

The router arms use their native catalog. OpenBench must not silently constrain
them to the fixed-baseline model set. Fixed baselines should include at least
one preregistered current coding SOTA model, pinned to an exact revision and
provider where the API permits it. The selection date and rationale are part of
the experiment artifact; "SOTA" is not inferred after results are known.

### Task taxonomy

The task set should be balanced and reported across preregistered strata:

| Dimension | Required labels |
|---|---|
| Work type | repair/debug, feature implementation, test/CI repair, integration/CLI, refactor/migration |
| Scope | local edit, multi-file, repository-wide |
| Tool demand | inspect-only, edit-and-test, iterative diagnosis |
| Context demand | small, medium, large |
| Difficulty | pilot-derived fixed-model discrimination band |

Every task must have deterministic ownership boundaries, an untouched-workspace
failure, a golden-solution pass, no checker dependency on model text, and a
declared taxonomy. Public headline sets must include multiple tasks per material
stratum; sparse strata are descriptive only. Difficulty labels come from a
separate pilot and are frozen before the router run.

### Matched scheduling

A **cell** is one arm, task, and repetition. A **matched block** contains every
router and fixed-model arm for one task and repetition. Arm order is randomized
within each block; blocks are balanced across multiple UTC windows to reduce
time-of-day and product-drift confounding. Each cell starts from the same clean
workspace and receives the same harness instructions, tools, sampling policy,
and budgets.

Repetitions and windows are preregistered from a precision target or a blinded
pilot. Selective reruns are forbidden. A budget cap, router refusal, exhausted
fallback chain, or checker failure is an observed treatment outcome and remains
in the denominator. Infrastructure or route-integrity invalidation replaces or
excludes the **whole matched block** under a preregistered maximum; exceeding
that maximum blocks publication.

## Adapter and route evidence contract

Each router adapter declares capabilities rather than being trusted by name:

- native router ID and protocol;
- selected-model and selected-provider response fields;
- trace lookup method, expected availability lag, and refresh deadline;
- candidate, retry, fallback, usage, cost, TTFB, semantic TTFT, and stream
  completion fields, each with an explicit support level;
- cache controls, client retry behavior, terminal-state semantics, and privacy
  scrub rules.

Before admission, the adapter must pass the live
[router evidence probe](router-evidence-probe.md) against its declared native
router. A capability snapshot and probe digest are frozen at experiment start.
The probe is repeated after the final window; material capability drift
invalidates publication or requires a new experiment.

For trace-capable products, the collector seals response evidence immediately,
then refreshes the trace with bounded backoff until the declared deadline. It
seals each trace revision and reconciles final model, provider, request/trace
identity, usage/cost where comparable, and retry/fallback events. A pending
trace is not silently downgraded at publication.

Every model call receives one evidence grade:

- `reconciled`: response facts and an independent post-request trace agree on
  selected model and provider.
- `observed`: the response exposes a provider-qualified selected model, but the
  adapter has no independent trace API.
- `unverifiable`: required identity is absent, contradictory, still pending, or
  cannot be joined to the request.

A cell is `reconciled` only when every call is reconciled. It is `observed` when
all calls are verifiable and at least one is response-only. Any unverifiable
call makes the cell unverifiable and invalidates its matched block. Reports
must stratify outcomes by grade and never present `observed` as `reconciled`.

Retry and fallback facts have independent coverage. An empty attempt list means
"no attempts were exposed," not "no retry occurred." Verified absence requires
a trace contract that represents the complete attempt chain.

## Metrics

The primary outcome is equal-task-weighted checker solve rate. Mean checker
score is co-primary only when every checker uses a preregistered compatible
partial-credit scale. Reports also include:

- selected model revision and provider distributions, with unknowns explicit;
- end-to-end cell latency plus per-call TTFB, semantic TTFT, and stream total;
- input, cached-input, reasoning, output, and total tokens with field coverage;
- cost per attempted cell and per solve, never mixing router-reported,
  invoice-reconciled, and frozen-list-estimate bases;
- retry/fallback incidence, chain length, terminal reason, and evidence
  coverage;
- call and cell availability, cap-hit incidence, route-integrity failures, and
  excluded/replaced matched blocks.

Missing telemetry reduces that metric's denominator; it never becomes zero.
Model/provider distributions are reported by task stratum as well as overall so
router behavior is not flattened into one catalog share.

Per-arm binary solve intervals use Wilson 95% intervals as descriptive
uncertainty. Primary router-minus-baseline and router-minus-router contrasts use
a paired, task-clustered bootstrap that resamples tasks and retains all
repetitions and arms within each sampled task. The same paired bootstrap covers
checker score, cell latency, token, and cost contrasts. Distribution shares use
task-clustered bootstrap intervals. Reports disclose task, repetition, call,
and evidence-coverage denominators and avoid a rank claim when the
preregistered comparison is not estimable.

## Sealed artifacts and publication

Raw prompts, outputs, transcripts, credentials, authorization headers, endpoint
URLs containing account IDs, and provider request IDs remain local. A public
privacy-safe bundle contains only:

- experiment, capability, task, checker, schedule, policy, and price digests;
- checker outcomes and minimized timing/token/cost ledgers;
- provider-qualified selected routes, evidence grades, hashed correlation IDs,
  minimized retry/fallback events, and trace-reconciliation checks;
- coverage and exclusion ledgers, metric definitions, report data, and a digest
  over the complete bundle.

Publication fails closed if task polarity, task or policy digests, complete
matched scheduling, fixed-baseline pins, price coverage for a cost claim,
privacy scanning, route identity, trace reconciliation deadlines, or bundle
digests fail verification. Partial or unverifiable runs may be retained
locally, but cannot produce a public ranking. Publication never upgrades,
imputes, or reconstructs missing route identity from output behavior.

## Admission gates

1. **Task gate:** polarity, determinism, provenance, taxonomy, and pilot
   discrimination are frozen.
2. **Adapter gate:** a fresh live probe proves every claimed field and its
   privacy-safe representation.
3. **Arm gate:** native routing is enabled for router arms; baseline model and
   provider revisions are fixed; harness and budgets match.
4. **Schedule gate:** all-arm blocks, windows, repetitions, seeds, replacement
   rules, and precision targets are preregistered.
5. **Artifact gate:** local raw evidence and public minimized evidence reconcile
   without exposing prompts, outputs, secrets, or account identifiers.
6. **Publication gate:** no unresolved or unverifiable block, digest mismatch,
   capability drift, selective rerun, mixed cost basis, or unsupported claim.

## Current evidence boundary

The committed 2026-07-27 conformance artifact is evidence for two draft
adapters, not a benchmark result:

- OpenRouter `openrouter/auto-beta` produced nine reconciled calls. Response
  metadata and generation traces agreed on selected model/provider; one call
  recorded a Baidu `429` followed by an Alibaba `200`.
- Concentrate `auto` produced nine response-observed, provider-qualified calls
  and no independent trace. All nine selected
  `anthropic/claude-opus-4-1` in this small probe; that is not a general routing
  distribution or quality claim.
- No equivalent native-routing evidence has been proven here for Vercel or
  Cloudflare. Their fixed-model Gateway Bench support does not admit them to
  Router Bench.

Remaining product decisions are the initial task mix and precision target, the
fixed SOTA baseline selection policy, whether response-observed arms may appear
in a headline table or only a separate evidence tier, and the maximum age of a
capability probe.
