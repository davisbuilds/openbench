# Harbor CountingProxy metering

OpenBench's Harbor Codex OAuth agent automatically routes each local Docker
trial through `CountingProxy` and seals independent usage evidence under the
trial's agent logs:

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

`obench.harbor_agents.codex:OpenBenchCodexOAuth` owns one
`HarborMeteringSession` per `run()`:

1. Bind `CountingProxy` on the Docker host with registered-cell enforcement.
2. Set the Codex runtime `OPENAI_BASE_URL` to
   `http://host.docker.internal:<port>/cell/<token>/codex/backend-api/codex`.
3. Run Codex and preserve the OAuth rotation through the existing cleanup hook.
4. In `finally`, derive agent totals from the Codex ATIF trajectory, drain the
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

Both sides use the same four counters:

| Counter | Agent evidence | Proxy evidence |
|---|---|---|
| Calls | Sum of ATIF-v1.7 `step.llm_call_count` | Sealed request-record count |
| Input | `total_prompt_tokens`, including cached input | Uncached + cache read + cache write |
| Cache | `total_cached_tokens` | Cache-read tokens |
| Output | `total_completion_tokens` | Output tokens |

Normalized row `turns` is not used as a call count. Pinned Harbor groups Codex
events by API call and assigns `llm_call_count = 1` to each resulting agent
step ([source](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/agents/installed/codex.py#L225-L293)).

The evidence status is:

- `exact`: the ledger is complete, all four counters are present, and all
  values match.
- `mismatch`: the ledger and reported counters are complete, but at least one
  value differs.
- `incomplete`: the seal/hash chain, any request usage, or any reported
  counter is missing or invalid.

`mismatch` and `incomplete` are evidence, not soft warnings.

## Import and publication hook

An importer should load the artifact, attach it to the normalized row, then
apply the publication gate:

```python
from obench.harbor_metering import (
    apply_to_imported_row,
    load_evidence,
    require_publication_eligible,
)

evidence = load_evidence(
    trial_agent_logs / "harbor-metering" / "harbor-metering.json"
)
row = apply_to_imported_row(row, evidence, proxy_required=True)
require_publication_eligible(evidence, proxy_required=True)
```

When `proxy_required=True`, only `exact` is eligible. Unknown schemas, invalid
statuses, `mismatch`, and `incomplete` fail closed. The importer should digest
`harbor-metering.json` into trial provenance before publication; wiring that
digest into `obench.harbor_results` and `obench.publish` remains outside this
module's allowed scope.

## Scope and limitations

- Local Harbor Docker only. Job scheduling and generic Harbor profiles are not
  part of this bridge.
- No model protocol translation and no TLS interception.
- Docker must resolve `host.docker.internal` to the host. This is native on
  Docker Desktop; Linux engines may need equivalent host-gateway setup.
- Live Docker/OAuth/model verification is intentionally not performed by the
  offline test lane.
