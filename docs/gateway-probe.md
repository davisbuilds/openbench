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

`publish` verifies every private artifact digest, requires every scheduled cold
and warm matched block, and recomputes the report before writing an exact public
file set. No public experiment snapshot is emitted because fields such as
endpoints, credential names, routing policy, and budget cannot all be
independently authenticated from sanitized result rows. A sanitized
`schedule.json` is emitted because its digest is already bound into every row;
verification uses it to authenticate exact case/repetition coordinates, arm
membership, prompt digests, and block IDs. Prompt digests remain in row
identities; receipt values and operational generation IDs are removed.
`verify` rejects partial schedules, extra files, symlinks, secrets, paths,
account identifiers, nonempty receipts, digest drift, schema drift, and reports
that do not exactly match recomputation.

Public report schema v4 has no qualitative sample-size classification. It
records complete and scheduled cold/warm blocks, p50/p95 values, metric
coverage, Wilson availability intervals, and paired bootstrap intervals.
Because legacy run bundles did not record trustworthy run commit or start/end
timestamps, those fields are explicitly `unknown`; the manifest separately
records the commit used to verify and project the bundle. Pass
`--verified-with-commit COMMIT` when publishing from an uncommitted verifier
checkout.
