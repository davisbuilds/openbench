# Investigation: expanding OpenBench to benchmark AI gateways / model routers

**Status:** exploratory (no code changes). Prepared in response to: "expand our
repo to be a framework that helps with benchmarking these gateways/routers
(Ramp, OpenRouter, Cursor, Databricks, Cognition, Vercel, ...)."

## TL;DR

- OpenBench today benchmarks **harnesses** on the axis *(harness, model)* — "same
  model, harness varies." Gateways/routers are a **different axis**: *same harness
  + same task, the serving/routing layer varies.*
- The mechanical seam already exists. The counting proxy
  (`obench/proxy.py`) plus the manifest fields `base_url_env` + `proxy_route`
  (`obench/candidates.py`) already inject an OpenAI/Anthropic-compatible base URL
  into a harness and meter every call. **A gateway is just a base URL + upstream.**
- The real work is not plumbing — it is (1) making **gateway** a first-class
  comparison dimension in the runner/report, and (2) capturing the metrics that
  actually differentiate gateways (which OpenBench does not record yet): the
  **actually-served model**, **latency/TTFT/throughput**, **cost with gateway
  markup**, and **failover/reliability**.
- Strategic fit is strong: it extends the signature "harness tax" story into a
  "**routing tax / gateway tax**" story, and OpenBench's checker-based
  correctness is a *unique* asset — it can prove whether a router's cost savings
  degrade task success, which pure latency/price dashboards cannot.

## What "gateway / router" means here

The named companies ship OpenAI/Anthropic-compatible endpoints that sit between
the client and one or more model providers:

- **Pass-through gateways** (Vercel AI Gateway, Databricks, Ramp's internal
  gateway, Cloudflare AI Gateway): one endpoint, unified auth/billing, caching,
  rate-limit smoothing, observability, spend controls. You still name the model.
- **Routers** (OpenRouter, Cursor, Cognition-style internal routers): you send a
  request and the layer **chooses** the model/provider — by price, load,
  availability, or a quality policy — and may transparently **fail over**.

The distinction matters for what you measure: a pass-through gateway is judged on
latency/cost/reliability at a fixed model; a router is *additionally* judged on
**routing quality** — did it pick a model that still solves the task, cheaply?

## How OpenBench works today (the relevant parts)

| Concept | Where | Relevance to gateways |
|---|---|---|
| **Arm = (harness, model)** aggregate key | `report.py:_arm_key` | Needs a gateway dimension to compare gateways. |
| **Cell = harness × task × trial** runner | `run.py:run_cell` | Unchanged; a gateway is orthogonal config. |
| **Counting proxy** — transparent pass-through, one JSONL ledger row per model call (usage, status, `duration_ms`, sampling) | `proxy.py` | This *is* the gateway measurement point. |
| **Proxy base-URL injection** for BYO harnesses via `base_url_env` + `proxy_route` | `candidates.py:run` (l.464), `run.py:_proxy_env` | The exact hook to route a harness through a gateway. |
| **Upstream registry** (openai/anthropic/chat/cursor/bridge/subbridge) | `proxy.py` route table | A gateway is a new upstream, or the proxy forwards to a gateway URL. |
| **Checker is sole judge** (exit 0 / `SCORE:`) | `run.py`, `validate_tasks.py` | Lets us score *correctness under routing* — the differentiator. |
| **Efficiency report** (wall time, token tax, Wilson CIs) | `report.py`, `stats.py` | Reusable; extend with latency/cost per gateway. |
| **Pricing** keyed by model | `report-pricing.sample.json` | Needs per-gateway pricing (markup) to compute cost. |

### The seam, concretely

For a manifest harness, the runner already does the equivalent of:

```python
# candidates.py run(), when env["OPENBENCH_PROXY"] is set
env[self.base_url_env] = f"{proxy_base}/cell/{token}/{self.proxy_route}"
```

so the harness talks to the proxy, and the proxy forwards to the configured
upstream. To insert a gateway you either (a) point `proxy_route` at a new
`gateway/<name>` upstream that forwards to the gateway's URL, or (b) inject the
gateway URL directly as `base_url_env` and keep the proxy in front for metering.
Both are small changes to existing machinery.

## What's genuinely missing (the actual work)

The plumbing exists; the **measurements that make gateways comparable do not**:

1. **Actually-served model.** For a *router*, the requested model ≠ served model.
   The proxy records `model` only from the **request** sampling
   (`proxy.py` l.399, `_collect_sampling`), never from the response body. To
   benchmark routers we must extract the response `model` field (JSON and SSE) and
   record a routing distribution per arm. **Gap.**
2. **Latency detail.** Only whole-cell `wall_time_s` and per-call `duration_ms`
   exist. Gateways compete on **TTFT** (time-to-first-token, needs streaming
   first-byte timing) and **throughput** (output tok/sec), plus p50/p95/p99. The
   proxy streams responses already (l.353-366) so first-token timing is a
   localized addition. **Gap.**
3. **Cost with gateway markup.** Pricing is per-model only. Gateways add markup or
   have their own price sheet; routers change which price applies per call. Need
   per-*(gateway, model)* pricing and a cost column. **Gap.**
4. **Reliability / failover.** The proxy already records `status` and `error`
   per call — enough to derive 5xx rate and error class. Failover (a retried call
   that lands on a different model/provider) needs the response-model capture from
   (1) plus a per-cell retry count. **Partial.**
5. **Cache behavior.** Usage fields include `cache_read`/`cache_write`; a
   cache-hit-rate metric per gateway is an aggregation, not new capture. **Easy.**
6. **Correctness under routing.** Already available via the checker — this is the
   headline metric and needs no new capture, only reporting it *alongside* the
   cost/latency it bought.

## Metric availability — verified against gateway docs (Jul 2026)

Both control (pin model / disable failover) and observability are available, so
the two benchmark modes above are real, not hypothetical.

**OpenRouter** (OpenAI-compatible; docs read directly):
- *Control:* `provider.allow_fallbacks: false` (no failover), `provider.only`
  (allow-list providers), `provider.order`, `provider.sort` = `price|throughput|latency`,
  `require_parameters`. Pin a single model + `allow_fallbacks:false` = deterministic
  fixed-model mode; omit it for router mode.
- *In-band per response* (`usage` object, always returned): served `model`,
  `prompt_tokens`, `completion_tokens`, `total_tokens`,
  `completion_tokens_details.reasoning_tokens`, `prompt_tokens_details.cached_tokens`,
  **`cost`** (credits charged) and `cost_details.upstream_inference_cost`.
- *Async per call* via `GET /generation?id=`: `latency` (ms to first token),
  `generation_time` (total), `provider_name`, `total_cost`, native token breakdown.

**Vercel AI Gateway** (OpenAI-compatible + AI SDK):
- *Control:* provider `order` / `only` and fallback control via provider options.
- *Observability:* dashboard + per-request metrics for requests, **TTFT (time to
  first token)**, input/output tokens, latency, and cost; usage also surfaced in
  the response / `providerMetadata`.

**Enterprise-gated (Ramp, Databricks, Cursor-internal, Cognition):** OpenAI/
Anthropic-compatible endpoints, but sign-up/keys are gated. Treat as
bring-your-own-endpoint; the served-model + tokens still come back in-band, and
anything they omit is still captured by our own counting proxy (status, latency,
tokens). So metric coverage degrades gracefully even without a vendor cost API.

**Bottom line:** served-model, tokens, cache, and (for OpenRouter/Vercel) cost +
TTFT are directly retrievable; where a vendor doesn't expose cost, our proxy still
gives tokens+latency+status and cost can be computed from a per-gateway price sheet.

## Design proposal

### A. Make `gateway` a first-class dimension

- Add a **gateway registry** (TOML), analogous to `--candidate`, e.g.
  `--gateway configs/openrouter.toml` declaring: display name, upstream base URL,
  auth env var, protocol (`openai`|`anthropic`|`responses`), optional
  fixed-model vs router mode, and pricing ref.
- Thread `gateway` through the results row (`ROW_FIELDS`) and the report key.
  **Decision required:** either
  - **A1 (safer):** encode gateway into the `model` string as `gateway/model`
    arms, so `_arm_key` stays `(harness, model)` and no aggregation code changes;
    or
  - **A2 (cleaner):** change `_arm_key` to `(harness, gateway, model)`. Note
    `report.py` aggregation is a **dangerous zone** in `AGENTS.md` — A2 touches it,
    A1 avoids it. Recommend prototyping with A1, migrating to A2 if it earns its
    keep.

### B. Extend the counting proxy (localized, stdlib-only)

- Extract **response model** from JSON and SSE bodies (mirror `extract_usage`),
  record as `served_model`.
- Record **TTFT** (timestamp of first streamed chunk vs request start) and
  **output throughput** (output tokens / stream duration).
- These reuse the existing capture loop and ledger row; no architectural change.

### C. Report additions (`report.py` / `stats.py`)

Per *(harness, gateway, model)* arm: solve rate + Wilson CI (existing), **cost per
solved task**, **latency p50/p95 + TTFT**, **routing distribution** (share of
calls per served model), **error/5xx rate**, **cache-hit rate**. Frame it as the
"gateway tax" table, parallel to the existing "harness tax" efficiency table.

### D. Seed configs + a gateway-oriented task angle

- Ship example gateway manifests for 2-3 of the named providers (OpenRouter and
  Vercel AI Gateway are the most open to sign up + document; Ramp/Databricks/
  Cognition are gated behind enterprise auth — treat as user-supplied).
- Existing tasks already stress correctness. Add a small tier that *exercises
  routing*: long-context (cache sensitivity), tasks solvable by a cheap model vs
  needing a frontier model (so routers can be caught under- or over-routing).

## Effort / risk

- **Small–medium.** The proxy injection, per-cell isolation, auth staging,
  Docker passthrough, Wilson CIs, and provenance already exist. New code is
  concentrated in: gateway registry/config, proxy response-model + timing capture,
  report axis + cost, and docs/seed configs.
- **Dangerous zones touched:** `report.py` arm key (if A2), `run.py` `ROW_FIELDS`
  (append-only — safe if appended), proxy ledger schema (append fields).
- **Non-goals stay intact** (`AGENTS.md`): still local CLIs + stdlib proxy, no
  cloud backend, no hosted leaderboard, checker contract preserved. A gateway
  layer fits the existing model cleanly.

## Strategic take

This is a natural, on-brand expansion rather than a pivot:

- It reframes the project's signature question ("how much does the layer around
  the model matter?") from *harness* to *gateway/router* — same methodology, new
  layer.
- **OpenBench's differentiator vs. every gateway's own latency/price dashboard is
  correctness.** Because a checker (not the model) judges success, OpenBench can
  answer the question those dashboards can't: *does this router's cheaper/faster
  routing actually still solve the task?* That "routing tax vs. correctness"
  quadrant is a defensible niche.
- Caveat: the marquee names (Ramp, Databricks, Cognition, Cursor internal) are
  enterprise-gated; the publicly reproducible seed panel will lean on OpenRouter /
  Vercel, with the others documented as bring-your-own-endpoint.

## Suggested phasing

1. **Spike (A1 + minimal proxy):** route one harness through OpenRouter via a
   gateway manifest; capture `served_model` + `duration_ms`; hand-report cost.
   Proves the routing-distribution + correctness story end-to-end.
2. **Productize:** gateway registry, TTFT/throughput capture, per-gateway pricing,
   the "gateway tax" report table, seed configs + docs.
3. **Decide** on A2 arm-key migration and a gateway-sensitive task tier once the
   spike shows which metrics actually separate gateways.

## Phase-1 spike — implemented (pi → OpenRouter, fixed-model)

The spike from step 1 is wired up (A1 arm scheme; no `report.py`/arm-key change).

**What shipped:**
- **Proxy gateway route.** `proxy.py` adds a `gateway/<name>` route with a
  `gateway_upstreams` registry (`DEFAULT_GATEWAY_UPSTREAMS = {"openrouter":
  "https://openrouter.ai/api/v1"}`, extensible via `--gateway-upstream name=url`).
  A cell's calls go to `…/cell/<token>/gateway/openrouter/<path>`; the proxy maps
  `openrouter` → the upstream and forwards the rest of the path unchanged.
- **Served-model + cost capture.** `proxy.extract_response_model` reads the model
  the gateway *actually served* from the response body (JSON and SSE; also the
  Responses API's `response.model`) — for a router this differs from the requested
  model. `proxy.extract_cost` pulls OpenRouter's in-band `usage.cost` and
  `usage.cost_details.upstream_inference_cost`. Both land on the ledger row
  (`served_model`, `cost`, `upstream_cost`), aggregated per cell in
  `run.apply_proxy_ledger` and appended to `ROW_FIELDS`. Existing redaction is
  unchanged (cost/model are not credential-like).
- **pi gateway models.** `adapters/pi.py` adds `GATEWAY_MODELS` (fixed-model, no
  router fallback) registered through the same provider extension as open models;
  only the proxy route differs (`GATEWAY_PROVIDERS` → `gateway/<name>`). Seed
  arms: `openrouter/openai/gpt-5.6`, `openrouter/anthropic/claude-sonnet-4.5`.
- **Runner wiring.** `run.PROXY_GATEWAY_MODELS` marks these pi cells
  proxy-supported and records the requested slug as sampling metadata.

**How to run it (once you have a key):**

```bash
export OPENROUTER_API_KEY=sk-or-...        # gateway key; one key, many providers
pip install -e .

# claude and gpt in the same OpenRouter run — differ only by the model string:
obench run --harness pi --model openrouter/anthropic/claude-sonnet-4.5 \
  --task make-it-run --proxy --trials 3
obench run --harness pi --model openrouter/openai/gpt-5.6 \
  --task make-it-run --proxy --trials 3
```

`--proxy` is required — it starts the counting proxy and injects the per-cell
base URL. Each cell's ledger row then carries `served_model`, `cost`,
`upstream_cost`, token usage, `duration_ms`, and `status` alongside the checker's
correctness verdict.

**Fixed-model vs router mode.** These seed arms are fixed-model: exactly one model
per arm, no fallback. Router mode (let OpenRouter choose / fall back, and measure
`served_model` distribution) is a follow-up — drop the single-model pin and pass
OpenRouter's `provider`/`models` controls through the provider extension.

**Not yet done (deferred to phase 2):** TTFT/throughput capture, per-gateway
price sheet, the "gateway tax" report table, seed configs for Vercel and BYO
enterprise endpoints, and the A2 `(harness, gateway, model)` arm-key decision.
