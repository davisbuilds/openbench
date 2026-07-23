# Router Bench

OpenBench has two separate benchmark families:

- **Harness Bench** asks how much the coding-agent harness matters when the
  underlying model and task are held fixed. Its top-level commands are
  `obench validate`, `obench doctor`, `obench run`, and `obench report`.
- **Router Bench** asks how much the serving route matters when the harness,
  model, provider, sampling, task, and budget are held fixed. Its command group
  is `obench router validate|doctor|run|report|publish|verify`.

Do not combine their result rows or interpret a Router Bench arm as another
harness. Router Bench currently supports only the **Gateway Tax** track with Pi
and the OpenAI Chat Completions streaming protocol.

## Tracks and status

| Track | Question | Status |
|---|---|---|
| Gateway Tax | What changes when the same model/provider is called directly versus through a gateway? | Implemented MVP |
| Provider Router | How well does a router choose among providers for one model? | Deferred; not implemented |
| Model Router | How well does a router choose among models? | Deferred; not implemented |

Gateway Tax uses one baseline `direct` arm and one or more `gateway` arms. Every
gateway arm names its `gateway` profile and `direct_control_arm_id`; direct arms
must not declare `gateway`. The schema requires each pair to
use the same canonical model revision, requested provider, provider allowlist,
protocol, and sampling. Each arm has its own endpoint-specific `requested_model`
wire ID and model allowlist. Fallbacks, gateway retries, and caching must all be
disabled. This
design measures the gateway path itself, rather than a gateway's model choice,
fallback policy, cache hit rate, or retry policy.

## Gateway profiles

Gateway arms must select one of these strict managed profiles:

| `gateway` | Endpoint and auth | Forced request controls | Required route evidence |
|---|---|---|---|
| `openrouter` | `https://openrouter.ai/api/v1/chat/completions`; Bearer token from `auth_env` | `provider.only`, `allow_fallbacks=false`, metadata enabled, cache disabled | OpenRouter requested/served model, selected provider, and strict attempt parsing when attempts are present |
| `vercel` | `https://ai-gateway.vercel.sh/v1/chat/completions`; Bearer token from `auth_env` (normally `AI_GATEWAY_API_KEY`) | provider-qualified model ID and only `providerOptions.gateway.only`; model fallbacks, order, sort, caching, and cache markers are removed | `finalProvider`, `resolvedProviderApiModelId`, single-attempt counts, and `modelAttempts.providerAttempts` |
| `cloudflare` | `https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1/chat/completions`; Bearer token from configurable `auth_env` | required nonsecret `gateway_id`, `cf-aig-skip-cache:true`, `cf-aig-max-attempts:1`, metadata-only logging, and an explicit provider/model wire ID | response-header provider and model; step, cache status, and privacy-safe log ID are captured when present |

Vercel evidence also retains privacy-safe `generationId`, `cost`, and
`marketCost` values when returned. This implementation does not make a paid
post-run generation lookup. Cloudflare does not call the Logs API; the response
must prove the provider and served model on its own. A requested Cloudflare
alias may resolve to a different exact revision only when that served revision
is declared in `allowed_models`. Missing or contradictory provider, model, or
attempt evidence fails route integrity closed.

`gateway = "concentrate"` is admission-blocked. Concentrate's current Chat
Completions contract cannot disable or prove fallback, retries, and caching, so
it cannot support strict Gateway Tax without weakening route integrity.

## Coding taskset

Router Bench runs the normal OpenBench coding tasks. Each arm receives a fresh
copy of the same task workspace, and `checker.sh` remains the sole judge of
correctness and partial credit. A useful starter taskset is:

- `make-ci-green`
- `add-feature`
- `misleading-error`

These tasks exercise multi-step code inspection and editing without the runtime
of the imported Terminal-Bench tier. Use multiple repetitions and time windows
to reduce sensitivity to transient provider load. The runner deterministically
counterbalances arm order within complete all-arm blocks. A report includes a
block only when every expected arm is present, infrastructure-valid, and passes
route integrity.

`run` executes only blocks whose declared UTC window is currently active. Run
the command again during each later window; completed blocks resume from the
same results file without changing the full schedule digest.

## Example: OpenAI direct vs OpenRouter/OpenAI

[`obench/examples/router-bench.toml`](../obench/examples/router-bench.toml)
compares the exact `gpt-4o-mini-2024-07-18` model:

- directly through OpenAI, using `OPENAI_API_KEY`;
- through OpenRouter while pinning provider `openai`, using
  `OPENROUTER_API_KEY`.

Both arms set temperature `0`, top-p `1`, and the same seed. The canonical
comparison model is `openai/gpt-4o-mini-2024-07-18`; the OpenAI wire ID omits
the provider prefix while the OpenRouter wire ID includes it. Both pin provider
`openai`; fallbacks, caching, and retries are disabled.

The TOML stores environment-variable **names only**, never credential values.
Export the two keys in the shell that runs `doctor` or `run`. For cost
validation, also provide a frozen price snapshot:

```bash
export OPENBENCH_ROUTER_FROZEN_PRICES_JSON='{
  "openai/gpt-4o-mini-2024-07-18": {
    "input_per_million": "0.15",
    "output_per_million": "0.60",
    "effective_at": "2026-07-22"
  }
}'
```

Pricing changes over time; review and deliberately update this snapshot before
each experiment. The example values are illustrative, not a current price
claim.

## Commands

Validate the experiment structure, task paths, workspace materialization, and
deterministic schedule without reading credentials or making model calls:

```bash
obench router validate obench/examples/router-bench.toml
```

Preflight credentials, Pi, tasks, and frozen prices without spending tokens:

```bash
obench router doctor obench/examples/router-bench.toml
```

Run or resume the scheduled blocks:

```bash
obench router run obench/examples/router-bench.toml \
  --results results/router-gpt-4o-mini-openrouter.jsonl
```

`execution_lane` selects the default lane; `--exec local|docker` overrides it.
The MVP currently executes only `local` and fails closed if `docker` is chosen.
An already valid latest block is skipped. `--force` appends a replacement block
attempt instead of rewriting prior evidence. Invalid paid blocks are never
retried automatically; an explicit later `run` is required.

Report matched, eligible blocks:

```bash
obench router report results/router-gpt-4o-mini-openrouter.jsonl
obench router report results/router-gpt-4o-mini-openrouter.jsonl --json
```

Create a sanitized evidence bundle, then verify every artifact digest and
public ledger chain:

```bash
obench router publish results/router-gpt-4o-mini-openrouter.jsonl \
  obench/examples/router-bench.toml results/router-gpt-4o-mini-bundle
obench router verify results/router-gpt-4o-mini-bundle
```

The runner durably persists the frozen price snapshot beside the results file,
so publishing does not depend on retaining the original shell environment.
Published rows retain their `exploratory` route-isolation classification.

Only `run` sends paid model requests. Invoking it is the explicit cost
authorization step. `budget.usd_cap`, `max_calls`, and `max_output_tokens` are
checked from the sealed cell ledger and can invalidate a cell, but they are not
a provider-side prepaid limit or a guarantee against overspend. In particular,
the USD check uses the frozen price estimate after observed calls. Start with a
small taskset, one repetition, and a low cap.

For tasks outside `tasks/`, pass the same `--tasks-dir PATH` to `validate`,
`doctor`, and `run`.

## Metrics and route evidence

Reports keep the direct and gateway arms separate and calculate:

- solve rate, mean checker score, availability, and task latency;
- time to first byte, semantic time to first token, and generation throughput;
- attempted cost and cost per solve for each available cost basis;
- served provider/model route distribution;
- paired gateway-minus-direct contrasts with task-cluster bootstrap intervals.

Metrics retain explicit task, cell, call, and cost-basis coverage. Missing timing
or pricing evidence does not silently become zero. Cost per solve is withheld
unless the selected cost basis covers every included call.

The managed proxy fixes the requested model, provider, and sampling on every
request and applies the selected gateway profile's retry, fallback, cache, and
evidence controls. OpenRouter preserves its existing strict metadata behavior.
Vercel requires documented routing and attempt metadata. Cloudflare requires
response-header evidence and never infers a provider from the requested model.
Route failures are recorded as reasons, and the entire all-arm block is
excluded from matched reporting. Ledgers contain
privacy-safe timing, usage, route identifiers, status, and hashes; they do not
retain prompts, generated text, or credential values.

## Secrets, proxy, and ledgers

At admission, declared key values are loaded into a memory-only secret plan.
The Pi subprocess receives a sanitized route plan, a synthetic proxy key, and a
cell-scoped proxy URL, not the direct or gateway credential. Gateway profile
controls remain inside the proxy and are bound by the committed arm digest. The
proxy strips client credential headers and uncontrolled cache or routing
fields, injects the admitted upstream credential, rejects redirects, and
forwards the fixed request.

Each cell has an append-only JSONL proxy ledger. Calls are sequence-bound and
hash-chained; the ledger is drained and terminally sealed before the checker
runs and before the result row is appended. Results bind the arm, task,
experiment, policy, pricing, schedule, sampling, harness version, execution
lane, and ledger seal by digest.

## Exploratory and verified-route eligibility

Route integrity and route isolation are different claims:

- **Route integrity** means the observed response evidence matches the committed
  arm. It is required for a block to enter the report.
- **Verified-route eligibility** would additionally require enforced outbound
  network isolation, so the harness cannot bypass the managed proxy.

The current local lane records `classification = "exploratory"` and
`egress_enforced = false`. Therefore current Router Bench results are
eligible only for **exploratory** analysis, not verified-route publication or
ranking.

The local lane runs the adapter on the host, has no image
digest, and inherits host CLI/runtime and network conditions. Use it for schema,
credential, and low-cost smoke work. Docker execution remains deferred until
its route and secret isolation contract can be enforced; do not mix execution
lanes in one comparison stratum.
