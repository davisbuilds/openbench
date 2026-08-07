# Computer-Use Bench v0

Computer-Use Bench v0 compares multiple MCP configurations while holding one
task, harness, and model identity fixed. It is a native macOS comparison lane.
It does not claim Harbor execution and it does not use an LLM judge.

## Plan

`obench.native_matrix.build_native_matrix()` creates the immutable comparison
plan. The default is five matched repetitions for a pilot. Public comparisons
should use at least ten repetitions.

Each repetition contains exactly one cell per MCP arm. Two arms run in AB/BA
orders; larger comparisons use deterministic forward/reverse rotations. The
plan binds canonical SHA-256 identities for the full plan, fixed
task/harness/model identity, each arm configuration, each cell, and each trial.

The plan is local execution intent and contains exact configuration identities.
Publish its digests, not the raw plan, when configuration values are private.

`reconcile_native_state()` accepts completed cell evidence only when its trial,
configuration, and cell identities match the plan. Repeating identical evidence
is idempotent. A different result or bundle digest for an occupied cell fails
instead of replacing it.

## Report

`obench.native_report.build_native_report()` accepts:

- exact normalized rows returned by the strict native importer; or
- native bundle directories that pass `load_native_trial()`.

Row inputs are checked for importer-equivalent verdict, timing, token, trial,
run, and identity invariants, then bound by a canonical normalized-row digest.
Because their bundle bytes are unavailable, their publication status remains
`complete_row_bound_bundle_not_revalidated`. Bundle inputs are the stronger
publication surface.

Bundle inputs add per-tool MCP counts, p50/p95 call latency, and categorical
error, outcome, delivery, and focus counts from the sealed privacy-safe ledger.
Row-only input cannot recover those details, so the report marks them
unavailable rather than deriving them from the total call count.

Only blocks containing every arm enter arm aggregates or matched deltas.
Missing cells and incomplete blocks remain visible in `coverage`. Duplicate
identical evidence is idempotent; conflicting normalized rows fail. When the
same exact row is supplied both directly and through a validated bundle, the
bundle-backed MCP detail is retained independent of input order.

The first declared arm is the matched-delta reference. Deltas are candidate
minus reference within the same repetition.

## Metrics

Binary success uses the deterministic verifier verdict and a Wilson 95%
confidence interval. Reward is verifier score only. Continuous metrics use the
median and nearest-rank p95:

- uncached input, cache read, cache write, output, and reasoning tokens;
- turns, wall time, agent time, and verifier time;
- retries and focus event totals;
- MCP calls and latency, including per-tool breakdowns when bundles are present.

Efficiency is successful trials divided by total fresh tokens, turns, or MCP
actions over the matched denominator. Fresh tokens are uncached input plus
output. The context-bloat proxy is total input (uncached plus cache-read) per
turn; cache-read share is reported separately. Missing measurements stay
missing and are never converted to zero.

## Public Boundary

The public report contains aggregates, methodology/configuration identities,
and the SHA-256 digests of each result and bundle manifest. The manifest digest
is the bundle identity because the strict importer verifies that the manifest
inventories and hashes every bundle file.

The report omits prompts, screenshots, ATIF trajectories, MCP payloads and
arguments, checker output, and raw token payloads. Publication validation
rejects absolute home paths, email addresses, secret-like values, and raw
evidence fields. Bundle seals detect mutation but do not attest an operator who
controls every input; `operator_evidence_attested` therefore remains false.
