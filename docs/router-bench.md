# Router Bench

OpenBench keeps three questions separate:

- **Harness Bench** compares coding-agent harnesses while holding the model and
  task fixed. It uses `obench validate|doctor|run|report`.
- **Gateway Tax** compares fixed routes to the same model/provider while holding
  the harness, sampling, task, and budget fixed.
- **Auto Router Bench** compares a prompt-aware model router with fixed-model
  controls drawn from the router's declared candidate pool.

Gateway Tax and Auto Router Bench share
`obench router validate|doctor|run|report|publish|verify`, but they are different
tracks (`gateway_tax` and `model_router`). Do not combine their rows. A gateway
tax result measures a serving path around a fixed model; a model-router result
measures a policy that may choose different models and fall back within a
declared pool.

Both tracks currently use Pi and the OpenAI Chat Completions streaming protocol.
The local execution lane is exploratory because it does not enforce outbound
network isolation.

## Fixed Gateway Tax

A `gateway_tax` experiment has exactly one baseline `direct` arm and one or more
`gateway` arms. Every gateway arm references the direct arm with
`direct_control_arm_id` and must match its canonical model, requested provider,
provider allowlist, protocol, and sampling. Client retries and caching are off,
and fallbacks are forbidden. This compares the direct, OpenRouter, or Vercel
serving path around the same rolling model alias instead of measuring provider
choice or recovery policy.

[`router-bench-three-way.toml`](../obench/examples/router-bench-three-way.toml)
is a matched three-way comparison:

1. OpenAI directly;
2. OpenRouter restricted to OpenAI;
3. Vercel AI Gateway restricted to OpenAI.

All three request the current `gpt-4o-mini` alias and use the canonical
comparison key `openai/gpt-4o-mini`. `model_match = "rolling_alias"` accepts
that provider-qualified alias or a dated snapshot that a provider reports after
resolving it. Other model families, conflicting provider qualifications, and
two different dated snapshots still fail route integrity.

This is intentionally a practical product comparison: it measures the
end-to-end experience of asking each gateway for the same rolling alias at
approximately the same time. It does not prove that every arm served the same
immutable model revision, so observed differences must not be attributed only
to gateway overhead. Matched-block scheduling reduces alias-roll timing risk.

Use `model_match = "exact_revision"` only for an optional stricter experiment
where every route accepts and reports the same immutable identifier. The legacy
`model_family` value remains accepted with the same normalization behavior as
`rolling_alias`.

### Gateway evidence

OpenRouter receives `provider.only = ["openai"]`,
`allow_fallbacks = false`, metadata opt-in, and cache-off controls. Its response
must identify the requested and served model, selected provider, and any
attempts. OpenRouter's response-reported cost is retained when present.

Vercel receives a provider-qualified model ID and only
`providerOptions.gateway.only = ["openai"]`; model fallbacks, provider ordering,
sorting, and cache controls are removed. Its documented provider metadata must
show the original model, final provider, resolved/canonical model, one model
attempt, one provider attempt, and success. `cost` and `marketCost` are retained
when returned. A valid timestamped `cost` becomes `router_reported` evidence,
while the independent frozen list-price estimate remains available for
comparison and budget enforcement in Gateway Tax.

Vercel supports routing one requested model across providers, provider filters,
fallbacks, and explicit model rewrite rules. Its documented cost-aware model
routing example puts a separate classifier in application code. As of
2026-07-22, Vercel documents no native prompt-aware model classifier comparable
to OpenRouter Auto Beta. OpenBench therefore treats Vercel as a fixed gateway
arm here, not an Auto Router Bench arm.

Concentrate can join this track once its integration is reproducible from a
documented request contract. Admission requires pinning both the OpenAI provider
and `gpt-4o-mini` alias, disabling or proving fallback, retries, and caching,
and returning evidence for the requested model, served model, provider, and
attempts. Provider-slug selection alone establishes only the provider lock.

## Auto Router Bench

A `model_router` experiment has exactly one OpenRouter Auto Beta arm and one or
more fixed OpenRouter controls. It permits no direct arm. This keeps gateway and
account effects common while asking whether prompt-aware model selection beats a
fixed model.

[`router-bench-auto.toml`](../obench/examples/router-bench-auto.toml) compares:

- `openrouter/auto-beta`, restricted to exactly
  `openai/gpt-5.1` and `anthropic/claude-haiku-4.5`;
- fixed `openai/gpt-5.1`, the declared baseline;
- fixed `anthropic/claude-haiku-4.5`.

Both candidate models support tool-using coding workflows. The auto arm's
`allowed_models` and `allowed_providers` must exactly equal the union of the
fixed controls. `cost_quality_tradeoff = 7` is committed into the experiment;
OpenRouter documents the scale as `0` for quality-first through `10` for
cost-first. Fixed controls disable fallback. Auto Beta enables only in-pool
router fallback; every arm still has cache off and client `retry_count = 0`.

The proxy overwrites caller routing fields and injects one opaque `session_id`
per benchmark cell. Auto Beta therefore pins its selected model and provider
across the multi-call Pi interaction for that task cell. A new arm/task/window/
repetition cell gets a new session, so stickiness cannot leak between matched
cells. Cache remains disabled even though OpenRouter also documents implicit
cache-based stickiness.

Reports show:

- solve rate, checker score, availability, latency, timing, throughput, and
  actual cost by arm;
- task-weighted served model/provider route distribution;
- attempt-evidence coverage, fallback call rate, and mean attempts per call;
- paired Auto-minus-baseline contrasts over complete matched blocks.

Route distribution answers which model/provider served calls. Attempt and
fallback metrics answer how often the router retried before success. They are
different signals and neither should be inferred from the other.

## Matched scheduling

Each arm receives a fresh copy of the same task workspace, and `checker.sh`
remains the sole judge. The runner deterministically counterbalances arm order
inside complete all-arm blocks keyed by task, window, and repetition. Reporting
includes a block only when every expected arm is present, infrastructure-valid,
and route-integrity-valid. It then weights calls to cells, complete blocks to
tasks, and tasks equally, with task-cluster bootstrap intervals.

Schema v1 requires absolute RFC3339 UTC windows; it has no relative
`next-week` form. The examples therefore contain clearly marked illustrative
future windows. Replace them with the intended non-overlapping windows before a
real run. `run` executes only currently active windows and resumes completed
blocks from the same results file.

## Three-way commands

Validation reads no secrets and makes no model calls:

```bash
python3 -m obench.cli router validate \
  obench/examples/router-bench-three-way.toml --tasks-dir tasks
```

Set environment-variable names referenced by the TOML and freeze the one
canonical model price. These rates are an illustrative list-price snapshot as
of 2026-07-22, not a promise that provider prices remain unchanged:

```bash
export OPENBENCH_ROUTER_FROZEN_PRICES_JSON='{
  "openai/gpt-4o-mini": {
    "input_per_million": "0.15",
    "output_per_million": "0.60",
    "effective_at": "2026-07-22"
  }
}'
```

Set `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, and `VERCEL_API_KEY` in the
shell that runs `doctor` or `run`. The TOML and commands name those variables
but never contain key values.

```bash
python3 -m obench.cli router doctor \
  obench/examples/router-bench-three-way.toml --tasks-dir tasks

python3 -m obench.cli router run \
  obench/examples/router-bench-three-way.toml \
  --tasks-dir tasks \
  --results results/router-gpt-4o-mini-three-way.jsonl

python3 -m obench.cli router report \
  results/router-gpt-4o-mini-three-way.jsonl

python3 -m obench.cli router publish \
  results/router-gpt-4o-mini-three-way.jsonl \
  obench/examples/router-bench-three-way.toml \
  results/router-gpt-4o-mini-three-way-bundle

python3 -m obench.cli router verify \
  results/router-gpt-4o-mini-three-way-bundle
```

## Auto Router commands

Validation is likewise offline:

```bash
python3 -m obench.cli router validate \
  obench/examples/router-bench-auto.toml --tasks-dir tasks
```

The Auto example needs only the OpenRouter key. Its frozen snapshot must cover
every model in the declared candidate pool because calls are priced by the
observed served model. These are illustrative list prices as of 2026-07-23:

```bash
export OPENBENCH_ROUTER_FROZEN_PRICES_JSON='{
  "openai/gpt-5.1": {
    "input_per_million": "1.25",
    "output_per_million": "10.00",
    "effective_at": "2026-07-23"
  },
  "anthropic/claude-haiku-4.5": {
    "input_per_million": "1.00",
    "output_per_million": "5.00",
    "effective_at": "2026-07-23"
  }
}'
```

Set `OPENROUTER_API_KEY` in the shell that runs `doctor` or `run`; do not put
its value in the experiment file.

```bash
python3 -m obench.cli router doctor \
  obench/examples/router-bench-auto.toml --tasks-dir tasks

python3 -m obench.cli router run \
  obench/examples/router-bench-auto.toml \
  --tasks-dir tasks \
  --results results/router-openrouter-auto-beta.jsonl

python3 -m obench.cli router report \
  results/router-openrouter-auto-beta.jsonl --json

python3 -m obench.cli router publish \
  results/router-openrouter-auto-beta.jsonl \
  obench/examples/router-bench-auto.toml \
  results/router-openrouter-auto-beta-bundle

python3 -m obench.cli router verify \
  results/router-openrouter-auto-beta-bundle
```

Only `run` sends paid model requests. It is the explicit cost-authorization
step. `usd_cap`, `max_calls`, and `max_output_tokens` are checked from the
sealed cell ledger; they are not provider-side prepaid limits and cannot
guarantee against overspend. Review prices and windows, then start with the
examples' one repetition and low caps.

## Secrets, ledgers, and publication

At admission, key values are loaded into a memory-only secret plan. Pi receives
a synthetic proxy key and cell-scoped proxy URL, not upstream credentials. The
proxy strips client credential, routing, retry, fallback, and cache fields and
injects the committed arm policy. Ledgers retain privacy-safe timing, usage,
route identifiers, status, cost, and hashes, not prompts, generated text, or
credential values.

Each cell ledger is append-only, sequence-bound, hash-chained, and terminally
sealed before the checker and result append. Results bind the arm, task,
experiment, policy, pricing, schedule, sampling, harness version, execution
lane, and ledger seal by digest. Publishing uses the persisted frozen-price
snapshot, sanitizes the evidence bundle, and verifies all artifact digests and
the public ledger chain.

The local lane records `classification = "exploratory"` and
`egress_enforced = false`. Docker execution remains deferred until route and
secret isolation can be enforced. Do not mix execution lanes in one comparison
stratum.

## References

- [OpenRouter Auto Router](https://openrouter.ai/docs/guides/routing/routers/auto-router)
- [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [OpenRouter router metadata](https://openrouter.ai/docs/guides/features/router-metadata)
- [Vercel AI Gateway models and providers](https://vercel.com/docs/ai-gateway/models-and-providers)
- [Vercel AI Gateway provider options](https://vercel.com/docs/ai-gateway/models-and-providers/provider-options)
- [Vercel cost-aware model routing](https://vercel.com/kb/guide/cost-aware-model-routing-with-ai-gateway)
- [Vercel routing rules](https://vercel.com/changelog/ai-gateway-routing-rules)
