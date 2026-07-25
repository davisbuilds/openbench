# Gateway Probe

Gateway Probe measures request serving through fixed direct and gateway routes.
It is separate from Gateway Bench: it runs no coding harness, task workspace, or
checker and makes no solve-rate or output-quality claim.

## Contract

Each matched block sends the same case, model, provider, sampling, and output
limit through every arm. Blocks are deterministically interleaved between:

- `cold`: one measured request on a fresh connection;
- `warm`: one unmeasured primer followed by the measured request on the verified
  same socket.

Primer and measured prompts use distinct deterministic nonces. The same nonce is
used across arms within a block, so connection reuse cannot be confused with
prompt or response cache reuse. Retries, redirects, fallback, and requested
caching are disabled.

The runner streams OpenAI Chat Completions or Responses directly. It retains only
derived DNS, TCP, TLS, request-to-first-byte, semantic TTFT, total duration,
throughput, usage/cache tokens, cost, route evidence, outcome classification, and
socket-reuse evidence. Prompt and output bytes are never written to results.
Responses requests set `store: false` to disable API application-state storage.
This does not override ordinary provider abuse-monitoring or safety-retention
policies.

Reports separate cold and warm conditions. They show scheduled, attempted,
successful, request-failed, route-verified, route-unverifiable, and route-failed
denominators. Arm metrics use p50/p95 with explicit coverage. Paired
gateway-minus-direct median contrasts use deterministic bootstrap confidence
intervals. Missing values are not zero-filled or trimmed. Runs with fewer than
100 complete blocks in either condition are labeled `exploratory`.

## Timing definitions

Cold request TTFB and semantic TTFT are end-to-end measurements starting
immediately before the resolver call and fresh connection setup. Warm request
timing starts immediately before request send on the established socket whose
primer and route were verified; primer DNS, TCP, and TLS setup is reported
separately and is not included in warm request latency.

DNS cache state is uncontrolled. `dns_s` is the observed duration of the local
resolver call, not proof that a cold request performed an uncached DNS lookup.

## Run

Start from the minimal two-way
[`gateway-probe-responses.toml`](../obench/examples/gateway-probe-responses.toml),
or use the
[`gateway-probe-four-way-responses.toml`](../obench/examples/gateway-probe-four-way-responses.toml)
comparison across direct OpenAI, OpenRouter, Vercel, and Concentrate. Set the
credentials declared by the selected experiment and a frozen price snapshot:

```bash
export OPENAI_API_KEY=...
export OPENROUTER_API_KEY=...
export OPENBENCH_GATEWAY_FROZEN_PRICES_JSON='{
  "openai/gpt-4o-mini": {
    "input_per_million": "0.15",
    "output_per_million": "0.60",
    "effective_at": "2026-07-25T00:00:00Z"
  }
}'
```

The prices above illustrate the required shape; verify current prices before a
real run.

```bash
obench gateway probe validate obench/examples/gateway-probe-responses.toml
obench gateway probe doctor obench/examples/gateway-probe-responses.toml
obench gateway probe run obench/examples/gateway-probe-responses.toml
obench gateway probe report results/gateway-probe-gpt-4o-mini-responses-probe.jsonl
```

`validate`, `doctor`, and `report` make no model API calls. `run` writes fsynced
JSONL and resumes only rows bound to the immutable experiment, schedule, and
price digests. The USD cap is cumulative across measured requests and paid warm
primers; execution stops after the first request that reaches the cap, or after
a request whose cost cannot be accounted. A partial latest block is replaced as
a complete new attempt.

Publication and verification bundles are not part of Gateway Probe v1.
