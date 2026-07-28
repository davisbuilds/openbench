# Router evidence probe

The router evidence probe is a pre-benchmark conformance check. It sends small
streaming requests through native automatic routers, records privacy-safe route
facts, and reconciles those facts against a post-request trace when the router
offers one.

It does not score router quality and is not a Router Bench result.

## Run

```bash
obench router evidence-probe \
  --router openrouter \
  --router concentrate \
  --repetitions 3 \
  --output results/router-evidence-probe.json

obench router evidence-verify results/router-evidence-probe.json
```

The OpenRouter probe requires `OPENROUTER_API_KEY`. The Concentrate probe
requires `CONCENTRATE_API_KEY`.

The repository includes the sanitized 2026-07-27 conformance artifact at
`data/router-evidence/2026-07-27-native-routing-spike.json`. Its 18 calls cover
three prompt classes repeated three times per router. OpenRouter reconciled 9/9
selected routes against its generation trace; Concentrate exposed 9/9 selected
routes in responses but no independent trace; no call was unverifiable.

The default cases cover an exact-output request, a small debugging request, and
a small architecture request. They use each router's native catalog rather than
an OpenBench model allowlist.

## Evidence statuses

- `reconciled`: response metadata and a post-request trace agree on the route.
- `observed`: the response exposes a provider-qualified selected model, but no
  independent trace API is available to the adapter.
- `unverifiable`: required identity is missing or contradictory.

The artifact omits prompts, model output, credentials, and authorization
headers. It stores prompt hashes, selected model/provider facts, usage, timing,
trace identifiers, reconciliation checks, and a digest over the complete
artifact.

OpenRouter is reconciled through its generation lookup. Concentrate is
currently response-observed because its public API does not expose a comparable
generation-by-ID retrieval endpoint.

The live conformance run also proved OpenRouter's declared model fallback
strategy and final selected route. One call exposed a provider retry from a
Baidu `429` to an Alibaba `200`; the response metadata and generation trace
agreed on both attempts and the final route. The other calls' empty attempt
lists are not proof that no retry occurred. A report must distinguish an empty
attempt list from a verified absence of retries.

## Acceptance before Router Bench

Router Bench may only depend on fields that pass this probe on live requests.
Missing or contradictory identity must remain an explicit evidence status.
Reports must not upgrade response-observed routes to reconciled routes or infer
model identity from latency or output behavior.

The proposed benchmark contract is documented in
[`router-bench.md`](router-bench.md). It treats this probe as adapter admission
evidence, never as a router-quality score.
