# Gateway Probe

Gateway Probe measures request serving through fixed direct and gateway routes.
It is separate from Gateway Bench: it runs no coding harness, task workspace, or
checker and makes no solve-rate or output-quality claim.

The headline multi-gateway probe uses managed gateway and billing routes,
including Cloudflare Unified Billing. Provider-native BYOK passthrough is not
mixed into that cohort because it uses a materially different upstream account
and billing path.

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

The runner streams OpenAI Chat Completions or Responses directly. Result rows
retain only derived phase timing, throughput, usage/cache tokens, cost, route
evidence, outcome classification, socket-reuse evidence, and allowlisted receipt
identifiers. Prompt and output bytes are never written to results or reports.
Responses requests set `store: false` to disable API application-state storage.
This does not override ordinary provider abuse-monitoring or safety-retention
policies.

The only response headers that can be persisted are `x-request-id`,
`request-id`, `openai-request-id`, `anthropic-request-id`, `x-vercel-id`, and
`cf-ray`. Values must be non-empty receipt identifiers using only ASCII letters,
digits, `.`, `_`, `:`, `/`, `@`, `+`, `=`, or `-`, at most 256 characters, and
unambiguous (duplicate fields are omitted). All other response headers are
discarded.

Reports separate cold and warm conditions. They show scheduled, attempted,
successful, request-failed, route-verified, route-unverifiable, and route-failed
denominators. Arm metrics use p50/p95 with explicit coverage. Paired
gateway-minus-direct median contrasts use deterministic bootstrap confidence
intervals. Missing values are not zero-filled or trimmed. Reports disclose the
complete and scheduled block counts for cold and warm conditions directly;
they do not assign a qualitative sample-size label.

## Timing definitions

Gateway Probe v3 does not call response-header availability "TTFB".
`http.client` does not expose a literal first-wire-byte timestamp, so the schema
uses observable boundaries:

- `setup_dns_s`, `setup_tcp_s`, and `setup_tls_s`: separate fresh-connection
  setup phases. They are measured-request metrics only for `cold`.
- `request_to_response_headers_s`: after the complete request body has been
  sent through the point where `getresponse()` has parsed the response headers.
- `request_to_first_body_byte_s`: after request send through the first
  non-empty SSE body chunk observed by the parser.
- `request_to_semantic_ttft_s`: after request send through the first semantic
  output delta.
- `request_stream_total_s`: after request send through stream consumption and
  the observed end of the response stream.
- `cold_end_to_end_response_headers_s`,
  `cold_end_to_end_first_body_byte_s`,
  `cold_end_to_end_semantic_ttft_s`, and
  `cold_end_to_end_stream_total_s`: the corresponding cold durations from one
  timestamp taken before DNS. These are absolute elapsed durations; setup is
  not summed into them again.

Warm measured-request timing contains only the four `request_*` metrics. The
primer must complete successfully with verified route evidence, and the
measured request must still have the same socket object, descriptor, and peer
immediately after dispatch. Primer DNS/TCP/TLS and receipt evidence live only
under `reuse_evidence`.

DNS cache state is uncontrolled. `dns_s` is the observed duration of the local
resolver call, not proof that a cold request performed an uncached DNS lookup.

## Run

Start from the minimal two-way
[`gateway-probe-responses.toml`](../obench/examples/gateway-probe-responses.toml),
or use the
[`gateway-probe-five-way-responses.toml`](../obench/examples/gateway-probe-five-way-responses.toml)
managed-gateway comparison across direct OpenAI, OpenRouter, Vercel,
Concentrate, and Cloudflare Unified Billing. Cloudflare's named gateway ID is
bound into the experiment and sent as `cf-aig-gateway-id`; its response cache
is skipped and its maximum attempt count is one. Set the credentials declared
by the selected experiment and a frozen price snapshot:

For a Chat Completions smoke run of Kimi K3 across Direct Moonshot and the same
four gateways, use
[`gateway-probe-kimi-k3-five-way-chat.toml`](../obench/examples/gateway-probe-kimi-k3-five-way-chat.toml).
It schedules five repetitions and caps each generated response at 128 tokens.
Replace its illustrative 32-hex Cloudflare account ID before running.

For the publish-sized DeepSeek V4 Flash comparison, use
[`gateway-probe-deepseek-v4-flash-five-way-chat.toml`](../obench/examples/gateway-probe-deepseek-v4-flash-five-way-chat.toml).
It schedules 50 cold and 50 warm requests per route, locks every gateway to the
DeepSeek provider, and seals thinking mode with `reasoning_effort = "high"` in
the experiment and public evidence. Chat Completions is used because all five
managed routes support that common protocol for this model.

For a production-like GPT-5.6 Sol comparison, use
[`gateway-probe-gpt-5.6-sol-five-way-responses.toml`](../obench/examples/gateway-probe-gpt-5.6-sol-five-way-responses.toml).
It uses the Responses API, fixes `reasoning.effort` to `medium`, and leaves the
sampling table empty so no temperature, top-p, or model seed is sent through
routes that do not share those controls. The deterministic `schedule_seed`
still balances and reproduces experiment ordering. The file schedules 50 cold
and 50 warm requests per route; use `--max-blocks 2` for the initial route
compatibility smoke, then resume the same output directory.

```bash
export OPENAI_API_KEY=...
export OPENROUTER_API_KEY=...
export VERCEL_API_KEY=...
export CONCENTRATE_API_KEY=...
export CLOUDFLARE_API_TOKEN=...
export OPENBENCH_GATEWAY_FROZEN_PRICES_JSON='{
  "openai/gpt-4o-mini": {
    "input_per_million": "0.15",
    "output_per_million": "0.60",
    "effective_at": "2026-07-25T00:00:00Z"
  }
}'
```

The Kimi K3 example uses `MOONSHOT_API_KEY` instead of `OPENAI_API_KEY`.
The DeepSeek V4 Flash example uses `DEEPSEEK_API_KEY` instead of
`OPENAI_API_KEY`.

The prices above illustrate the required shape; verify current prices before a
real run.

Run the complete preflight, resumable benchmark, and report workflow:

```bash
obench gateway probe benchmark \
  obench/examples/gateway-probe-responses.toml
```

By default this creates a fresh UTC timestamped directory:

```text
results/gateway-probe-<experiment-id>-<timestamp>/
  experiment.toml
  prices.json
  results.jsonl
  report.md
  report.json
  manifest.json
```

`experiment.toml` is the exact input snapshot and therefore contains the
configured case prompts. Generated results, reports, and the manifest do not
contain prompt or output content. `prices.json` is the parsed non-secret frozen
price table; no other environment values are persisted. The manifest binds the
five content files with SHA-256 digests.

Use an explicit stable directory for CI and resume:

```bash
obench gateway probe benchmark \
  obench/examples/gateway-probe-responses.toml \
  --output-dir results/gateway-probe-ci
```

For long experiments with short-lived credentials, bound each invocation to a
fixed number of complete matched blocks and resume against the same directory:

```bash
obench gateway probe benchmark \
  obench/examples/gateway-probe-responses.toml \
  --output-dir results/gateway-probe-ci \
  --max-blocks 10
```

`--max-blocks` accepts a positive integer on both `benchmark` and the lower-level
`run` command. It stops only after complete matched blocks. A complete
replacement attempt consumes one block from the invocation bound; already
complete blocks skipped during resume do not. Repeating the command preserves
the experiment, schedule, price, and row identities and continues with the next
unfinished schedule coordinate. Omitting the option retains the unbounded
behavior.

On resume, the exact experiment and canonical price snapshots must match the
existing directory. The results JSONL is fsynced and resumes only rows bound to
the immutable experiment, schedule, and price digests. Reports and the manifest
are invalidated before execution and regenerated atomically after a successful
run/resume, so a failed resume cannot leave stale reports labeled as current.
Valid budget-stopped partial runs still produce reports; unavailable arms and
paired metrics have explicit zero coverage. The USD cap is cumulative across
measured requests and paid warm primers; execution stops
after the first request that reaches the cap, or after a request whose cost
cannot be accounted. A partial latest block is replaced as a complete new
attempt.

A bounded `benchmark` invocation also regenerates the local report and manifest.
The report's `complete_blocks` and `scheduled_blocks_per_condition` values show
that the experiment is partial; the local manifest only binds artifact digests
and does not assert completion. `publish` remains fail-closed until every
scheduled cold and warm block is complete.

The lower-level `validate`, `doctor`, `run`, and `report` commands remain
available. `validate`, `doctor`, and `report` make no model API calls.

## Publish

Private benchmark bundles are not documentation-safe: `experiment.toml`
contains prompts, endpoints, credential environment names, and potentially
account identifiers. Project a completed run into a separate public bundle:

```bash
obench gateway probe publish RUN_DIR BUNDLE_DIR
obench gateway probe verify BUNDLE_DIR
```

`publish` verifies every private artifact digest, parses the exact private
experiment, requires every scheduled cold and warm matched block, and
recomputes the report before writing an exact public file set. Canonical
`experiment.json` retains the experiment and arm digests plus public-safe
controls: repetitions, schedule seed, model matching, timeout, output-token and
spend ceilings, case IDs and prompt digests, protocol/model/provider selection,
sampling, direct-control relationships, and fallback/retry/cache settings.
Endpoints, credential environment names, named gateway IDs, account IDs,
private allowlists, prompt text, and secrets are omitted.

The manifest hashes `experiment.json`. Verification validates its exact schema
and canonical encoding, reconstructs `schedule.json` from its retained controls,
and binds its experiment, case, and arm digests to the public rows. The
sanitized schedule authenticates exact case/repetition coordinates, arm
membership, prompt digests, and block IDs. Receipt values and operational
generation IDs are removed. `verify` rejects partial schedules, extra files,
symlinks, secrets, paths, account identifiers, nonempty receipts, digest drift,
schema drift, experiment/schedule/row binding drift, and reports that do not
exactly match recomputation.

Public report schema v4 has no qualitative sample-size classification. It
records complete and scheduled cold/warm blocks, p50/p95 values, metric
coverage, Wilson availability intervals, and paired bootstrap intervals.
Because legacy run bundles did not record trustworthy run commit or start/end
timestamps, those fields are explicitly `unknown`; the manifest separately
records the commit used to verify and project the bundle. Pass
`--verified-with-commit COMMIT` when publishing from an uncommitted verifier
checkout.
