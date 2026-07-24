# Gateway Bench

Gateway Bench compares AI API gateways while holding the coding harness, task,
model family, inference provider, sampling, budgets, and repetition schedule
fixed. It measures the path through a gateway against the provider's direct API
and against other gateways.

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

Examples:

- [`gateway-bench-responses.toml`](../obench/examples/gateway-bench-responses.toml):
  direct OpenAI versus Cloudflare using the Responses API.
- [`gateway-bench-five-way-responses.toml`](../obench/examples/gateway-bench-five-way-responses.toml):
  direct OpenAI, OpenRouter, Vercel, Concentrate, and Cloudflare.
- [`gateway-bench-four-way.toml`](../obench/examples/gateway-bench-four-way.toml):
  Chat Completions variant without Concentrate.

## Route Locking

The counting proxy replaces caller-controlled routing and sampling fields before
forwarding a request.

- Direct OpenAI: fixed model ID and sampling; no gateway controls.
- OpenRouter: `provider.only` contains one provider and
  `allow_fallbacks=false`.
- Vercel: `providerOptions.gateway.only` contains one provider.
- Concentrate: `routing.providers` contains one provider and no alternate
  models.
- Cloudflare: cache is skipped and maximum attempts is one; the requested model
  remains provider-qualified.

Client retries are zero. Request cache controls are stripped. A returned
cached-input count is retained as measured provider behavior, not treated as a
client-requested cache hit.

Route integrity fails closed when required evidence is absent or contradictory:
served model, provider, requested-model metadata, terminal stream state, or
single-attempt evidence where the gateway exposes it.

## Metrics

Reports retain per-metric coverage and use equal task weighting.

- checker solve rate and mean checker score;
- availability and end-to-end cell latency;
- time to first byte and semantic time to first token;
- output throughput;
- input, output, and total token usage;
- cached-input and cache-write token counts when reported;
- cost using separately labeled evidence bases;
- served-model/provider distribution;
- route-integrity and infrastructure exclusion reasons;
- paired gateway-minus-direct contrasts.

Cost bases are never silently mixed:

- `gateway_reported`: amount returned by the gateway;
- `invoice_reconciled`: later billing reconciliation, when supplied;
- `frozen_list_estimate`: token usage priced from the experiment's frozen
  price snapshot.

Missing timing, token, cache, or cost evidence reduces only that metric's
coverage. It does not become zero.

## Protocols

Gateway Bench supports:

- `openai_chat` endpoints ending in `/chat/completions`;
- `openai_responses` endpoints ending in `/responses`.

Concentrate is admitted only through its Responses endpoint. Cloudflare supports
its AI Gateway REST route for both protocols and its compatibility route for
Chat Completions. Public experiments require strict known endpoints; private
test endpoints require `allow_private_endpoint=true` plus an explicit hostname
or CIDR allowlist.

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
digests.

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
