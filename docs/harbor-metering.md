# Harbor CountingProxy metering

OpenBench's Harbor Codex and Pi OAuth agents automatically route each local
Docker trial through `CountingProxy` and seal independent usage evidence under
the trial's agent logs:

```text
agent/
└── harbor-metering/
    ├── harbor-metering.json
    └── private/
        ├── <cell-token>.jsonl
        └── <cell-token>.meta.json
```

`harbor-metering.json` is the importer/publication artifact. `private/` is
mode `0700`; its files are mode `0600` and remain local forensic inputs.

## Lifecycle

Each supported profile owns one `HarborMeteringSession` per `run()`:

1. Bind `CountingProxy` on the Docker host with registered-cell enforcement.
2. Set the runtime endpoint to a tokenized local route. Codex uses
   `/codex/backend-api/codex`; Pi writes `/codex/backend-api` into its isolated
   `openai-codex.baseUrl`.
3. Run the harness and preserve its OAuth rotation through the profile cleanup
   hook.
4. In `finally`, derive agent totals from the ATIF trajectory, drain the
   proxy, verify its hash chain and terminal seal, write the evidence artifact,
   and stop the listener.

For multiple sequential trials sharing one host OAuth staging context, configure
the Harbor agent with `concurrency_group` limited to `1`. At each supported
pre-cleanup boundary the wrapper downloads the rotated `auth.json` into the
return file, atomically refreshes the staged input from that copy, and retains
the return file. The next sequential trial therefore starts from the previous
rotation, while the final return remains available for the host credential
owner's compare-and-swap persist-back.

The tokenized endpoint is assigned directly to the in-memory agent instance.
It is not passed in Harbor argv, `--ae`, job config, or lock files. The public
evidence artifact also omits it. The retired external integration API keeps a
template-based `agent_env` hook for future non-agent callers, but the OAuth
agent does not need it.

## Routing and credentials

This is ordinary endpoint routing, not TLS interception:

```text
Codex container --HTTP--> CountingProxy --HTTPS--> chatgpt.com
```

The proxy strips the local `/cell/<token>/codex` routing prefix and forwards
the normal `/backend-api/codex/...` request path. It forwards Codex's
end-to-end `Authorization`, `ChatGPT-Account-Id`, and origin headers to
ChatGPT, but never writes request headers or bodies to its ledger. Ledger
records contain normalized usage, status, timing, model/sampling metadata, and
hashed conversation links only. OAuth credentials and raw prompts are not
persisted.

Pinned Harbor `0.20.0` source at commit
[`72bc40b1e58b47a9cc6e0f14c29aced3a9e53767`](https://github.com/harbor-framework/harbor/tree/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767)
proves the supported configuration:

- [`codex.py` lines 1022-1035](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/agents/installed/codex.py#L1022-L1035)
  applies `OPENAI_BASE_URL` as Codex `openai_base_url`.
- [`codex.py` lines 1079-1107](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/agents/installed/codex.py#L1079-L1107)
  selects `auth.json` independently of the endpoint.
- [`codex.py` lines 1137-1183](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/agents/installed/codex.py#L1137-L1183)
  passes the endpoint to Codex's environment and effective config; Harbor does
  not inject alternate OAuth headers.

Therefore Codex OAuth is configurable through its base URL. Codex continues
to create its required OAuth/account/origin headers from `auth.json`; the
transparent proxy forwards them unchanged.

## Reconciliation

Token totals are reconciled across both surfaces; calls come from the sealed
proxy ledger because ATIF-v1.7 does not define a standard aggregate call count:

| Counter | Agent evidence | Proxy evidence |
|---|---|---|
| Calls | Optional `step.llm_call_count` extension | Sealed request-record count |
| Input | `total_prompt_tokens`, including cached input | Uncached + cache read + cache write |
| Cache | `total_cached_tokens` | Cache-read tokens |
| Output | `total_completion_tokens` | Output tokens |

Normalized row `turns` is not used as a call count. When ATIF omits the optional
extension, the reconciliation field is `proxy_only`; the sealed, hash-chained
ledger remains the independently recomputed call-count source.

Only `POST .../responses` and `POST .../chat/completions` records are model
calls. Endpoint probes and model-discovery requests remain in the ledger as
auxiliary requests, but they are not misreported as generations and do not
require token usage. Public provenance retains total, model-call, and auxiliary
request counts.

The evidence status is:

- `exact`: the ledger is complete and all ATIF token totals match. A missing
  optional ATIF call count does not weaken the ledger-derived call count.
- `mismatch`: the ledger and reported counters are complete, but at least one
  value differs.
- `incomplete`: the seal/hash chain, any request usage, or any reported
  counter is missing or invalid.

`mismatch` is retained evidence, not a soft warning: both Harbor and proxy
values remain on the normalized row, but the row is excluded from token, cost,
and efficiency rankings. `incomplete` remains a hard integrity failure for a
proxy-required profile.

## Import and publication hook

`obench import harbor-results` verifies the private ledger, checks its seal and
hash chain, reconciles it with ATIF, digests the public and private evidence,
and attaches the publication gate to the normalized row:

```python
from obench.harbor_metering import (
    apply_to_imported_row,
    require_publication_eligible,
    verify_evidence_dir,
)

evidence = verify_evidence_dir(
    trial_agent_logs / "harbor-metering",
    expected_trial_id=trial_id,
    expected_harness=harness,
)
row = apply_to_imported_row(row, evidence, proxy_required=True)
require_publication_eligible(evidence, proxy_required=True)
```

When `proxy_required=True`, `exact` is publication- and ranking-eligible and is
labeled `Harbor-reported + proxy-verified`. A structurally valid `mismatch` is
publication-eligible but ranking-ineligible; publication preserves both values
and displays a prominent warning. Unknown schemas, invalid statuses,
`incomplete`, malformed records, broken seals, and ledger tampering fail closed.
Non-proxy profiles publish available ATIF totals as `Harbor-reported`.

## Scope and limitations

- Local Harbor Docker only. Harbor owns job scheduling.
- No model protocol translation and no TLS interception.
- Docker must resolve `host.docker.internal` to the host. This is native on
  Docker Desktop; Linux engines may need equivalent host-gateway setup.
- Offline CI never uses Docker, OAuth, or a model. Release evidence is produced
  by an explicit benchmark-host run from an exact pushed commit.
