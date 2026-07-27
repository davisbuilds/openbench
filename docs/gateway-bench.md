# Gateway Bench

Gateway Bench compares AI API gateways while holding the coding harness, task,
model family, inference provider, sampling, budgets, and repetition schedule
fixed. It measures the path through a gateway against the provider's direct API
and against other gateways.

The headline comparison uses each gateway's managed service and billing path.
Cloudflare therefore uses its Unified Billing REST API, not provider-native
BYOK passthrough. Provider-native experiments answer a different question and
must be published as a separate track.

This is not a model-router benchmark. A router chooses a model or provider for
each request. Gateway Bench forbids that behavior: every arm must request one
declared model and one declared provider, with fallback, retries, and request
caching disabled. Automatic routing will use a separate future `obench router`
surface and result schema.

## Comparison Contract

An experiment uses the single track `fixed_model_provider` and contains:

- exactly one baseline `direct` arm;
- one or more `gateway` arms, each referencing the direct control;
- the same harness, tasks, model family, provider, sampling, and budgets;
- a deterministic randomized schedule of matched all-arm blocks;
- fresh workspaces and sealed proxy ledgers for every cell.

A **cell** is one task, repetition, and arm. A **matched block** contains all
arms for one task and repetition. If infrastructure invalidates one cell, the
whole block is excluded or replaced so every paired comparison uses the same
task opportunity.

Reaching `budget.max_calls` is a valid treatment outcome, not infrastructure
failure: the cell remains in the denominator as unsolved with score zero.
Never rerun only a cap-hit arm after observing its result. A different call
budget is a separate experiment; any explicit replacement must rerun the
complete all-arm block for a recorded infrastructure or route-integrity
invalidation.

Examples:

- [`gateway-bench-responses.toml`](../obench/examples/gateway-bench-responses.toml):
  direct OpenAI versus Cloudflare using the Responses API.
- [`gateway-bench-five-way-responses.toml`](../obench/examples/gateway-bench-five-way-responses.toml):
  direct OpenAI, OpenRouter, Vercel, Concentrate, and Cloudflare.
- [`gateway-bench-four-way.toml`](../obench/examples/gateway-bench-four-way.toml):
  Chat Completions variant without Concentrate.
- [`gateway-bench-kimi-k3-five-way-chat.toml`](../obench/examples/gateway-bench-kimi-k3-five-way-chat.toml):
  Kimi K3 over Chat Completions through Direct Moonshot, OpenRouter, Vercel,
  Concentrate, and Cloudflare. Its 12,000-token budget is an aggregate
  coding-agent cell cap; use the 128-token Gateway Probe example for the cheap
  request-level smoke check.

## Route Locking

The counting proxy replaces caller-controlled routing and sampling fields before
forwarding a request.

- Direct OpenAI: fixed model ID and sampling; no gateway controls.
- OpenRouter: `provider.only` contains one provider and
  `allow_fallbacks=false`.
- Vercel: `providerOptions.gateway.only` contains one provider.
- Concentrate: `routing.providers` contains one provider and no alternate
  models. The Kimi K3 profile translates the cross-route `moonshotai` identity
  to Concentrate's `moonshot` provider slug.
- Cloudflare Unified Billing: the named managed gateway is bound into the
  experiment and sent as `cf-aig-gateway-id`; cache is skipped, maximum
  attempts is one, and the requested model remains provider-qualified.

Client retries are zero. Gateway response-cache controls are disabled or
stripped. A returned cached-input count is retained as provider-reported
prompt-prefix behavior, not treated as a gateway response-cache hit.

Provider prompt-prefix caching is a required, experiment-wide condition:

- `provider_prompt_mode = "provider_default"` leaves the provider-visible
  prompt intact and measures normal production behavior, including
  provider-reported cached input tokens.
- `provider_prompt_mode = "isolated_per_call_v1"` prepends a unique neutral
  identifier to Responses `instructions` on every model call. It is currently
  admitted only for direct OpenAI, OpenRouter, Vercel, and Concentrate
  Responses routes. A cell fails route integrity if transformation evidence is
  missing or the provider reports any cached input tokens.

The provider prompt mode is bound into experiment, policy, run, and cell
identity, retained in public bundles, and reports refuse to mix modes. Use
`provider_default` for headline gateway comparisons and
`isolated_per_call_v1` as a sensitivity control for whether provider prompt
caching changes the ranking. Both modes disable gateway response caching.

Route integrity fails closed when required evidence is absent or contradictory:
served model, provider, requested-model metadata, terminal stream state, or
single-attempt evidence where the gateway exposes it.

## Metrics

Reports retain per-metric coverage and use equal task weighting.

- checker solve rate and mean checker score;
- availability and task-weighted median end-to-end cell latency;
- time to first byte and semantic time to first token;
- output throughput;
- input, output, and total token usage;
- cached-input and cache-write token counts, cache-hit call rate, and cached
  input fraction when reported;
- cost using separately labeled evidence bases;
- served-model/provider distribution;
- route-integrity and infrastructure exclusion reasons;
- per-arm cap-hit cells and cap-affected matched blocks;
- paired gateway-minus-direct contrasts.

Cell latency is summarized as the median across repetitions within each task,
then the median across tasks. Its paired contrast applies the same aggregation
to matched gateway-minus-direct differences. This keeps long agent trajectories
from dominating the headline while preserving equal task weighting. It is
end-to-end benchmark-cell latency, including the agent trajectory and timeout
cap. TTFB, semantic TTFT, and output throughput are per-call serving telemetry
with independent call-coverage denominators; they are not interchangeable with
the cell-latency headline.

Cost bases are never silently mixed:

- `gateway_reported`: amount returned by the gateway;
- `invoice_reconciled`: later billing reconciliation, when supplied;
- `frozen_list_estimate`: token usage priced from the experiment's frozen
  price snapshot.

The public cross-route cost column uses `frozen_list_estimate` only when that
basis completely covers every arm. It never fills missing arms from
`gateway_reported` or `invoice_reconciled`. The frozen estimate applies its
dated input and output rates to reported token usage. Unless the frozen price
contract explicitly defines a separate cache rate, reported cached input is
still priced at the frozen input rate; the estimate is therefore a standardized
comparison basis, not an invoice or a claim about each provider's cache
discount.

Paired frozen-list cost contrasts are cost per attempted cell, including failed
attempts, not cost per solve. Per-solve cost remains an arm-level outcome and is
shown only with complete call and cell coverage.

Missing timing, token, cache, or cost evidence reduces only that metric's
coverage. It does not become zero.

## Protocols

Gateway Bench supports:

- `openai_chat` endpoints ending in `/chat/completions`;
- `openai_responses` endpoints ending in `/responses`.

Concentrate is admitted through its exact `/chat/completions` and `/responses`
endpoints for the matching protocol. Cloudflare is admitted only through its
managed Unified Billing REST route for both protocols; provider-native and
compatibility/BYOK routes belong in separate experiments. Public experiments
require strict known endpoints; private test endpoints require
`allow_private_endpoint=true` plus an explicit hostname or CIDR allowlist.

## Run

Set only the credentials declared by the chosen experiment. For the five-way
Responses example:

```bash
export OPENAI_API_KEY=...
export OPENROUTER_API_KEY=...
export VERCEL_API_KEY=...
export CONCENTRATE_API_KEY=...
export CLOUDFLARE_API_TOKEN=...
```

The Kimi K3 Chat Completions example uses the same gateway credentials and
`MOONSHOT_API_KEY` for its direct control.

Gateway Bench also requires a frozen price snapshot so the USD cap can be
enforced before and during the run. Each canonical model used by an arm needs
input and output prices per million tokens plus the price timestamp:

```bash
export OPENBENCH_GATEWAY_FROZEN_PRICES_JSON='{
  "openai/gpt-4o-mini": {
    "input_per_million": "0.15",
    "output_per_million": "0.60",
    "effective_at": "2026-07-24T00:00:00Z"
  }
}'
```

Use prices verified for the publication date; the values above are an example
of the required format, not a current pricing claim. `doctor` fails closed if a
canonical model is missing.

Validate task polarity and experiment structure:

```bash
obench gateway validate \
  obench/examples/gateway-bench-five-way-responses.toml \
  --tasks-dir tasks
```

Check credentials, model/provider locks, task provenance, and frozen price
coverage without spending model tokens:

```bash
obench gateway doctor \
  obench/examples/gateway-bench-five-way-responses.toml \
  --tasks-dir tasks
```

Run or resume the experiment:

```bash
obench gateway run \
  obench/examples/gateway-bench-five-way-responses.toml \
  --tasks-dir tasks \
  --results results/gateway-five-way.jsonl
```

Generate a task-weighted report:

```bash
obench gateway report results/gateway-five-way.jsonl
obench gateway report results/gateway-five-way.jsonl --json \
  > results/gateway-five-way-report.json
```

## Publish

Publication creates a new sanitized, tamper-evident `gateway_bench` bundle. It
contains canonical Gateway identities (`benchmark="gateway"`), `gateway-run-*`
and `gateway-cell-*` IDs, public snapshots, minimized ledgers, and provenance
digests. Publishing and verification reject a result set that does not contain
the latest complete all-arm block for every declared task, window, and
repetition.

```bash
obench gateway publish \
  results/gateway-five-way.jsonl \
  obench/examples/gateway-bench-five-way-responses.toml \
  results/gateway-five-way-bundle

obench gateway verify results/gateway-five-way-bundle
```

Raw transcripts, credentials, endpoint URLs, account IDs, and request or
response bodies are not publishable. Existing Router-named scratch artifacts
are not converted: rerun the experiment to produce Gateway-native evidence
before publication.
