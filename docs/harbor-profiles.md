# Harbor harness profiles

OpenBench defines pinned Harbor `0.20.0` agent profiles for controlled
harness-vs-harness comparisons. A profile resolves one canonical OpenBench
`(harness, model)` arm into Harbor `AgentConfig` inputs. Harbor still owns job
creation, trial scheduling, retries, environments, verification, and artifacts.

## Compatibility matrix

| Harness | Harbor agent import | CLI version | Harbor model | Behavior | OAuth |
|---|---|---:|---|---|---|
| Codex | `obench.harbor_agents.codex_profile:OpenBenchCodexOAuthProfile` | `0.144.5` | `<model-id>` | `reasoning_effort=medium`; apps, plugins, and multi-agent disabled; GPT-5.6 service tier `default` | `~/.codex/auth.json` |
| Pi | `obench.harbor_agents.pi:OpenBenchPiOAuth` | `0.80.10` | `openai-codex/<model-id>` | `thinking=medium`; `--no-approve`; `--no-extensions`; isolated Pi home | `~/.pi/agent/auth.json` |
| OpenCode | `obench.harbor_agents.opencode:OpenBenchOpenCodeOAuth` | `1.18.3` | `openai/<model-id>` | `variant=medium`; permission bypass; isolated HOME/XDG config, data, state, and cache | first existing OpenCode auth candidate |
| Cursor | `obench.harbor_agents.cursor:OpenBenchCursorSubscription` | `2026.07.09-a3815c0` | canonical OpenBench model | native Cursor model UID; `--force`; `--trust`; isolated HOME/XDG | first existing Cursor auth candidate |
| Devin | `obench.harbor_agents.devin:OpenBenchDevinSubscription` | `3000.2.17` | canonical OpenBench model | native Devin model UID; `dangerous` permission mode; isolated HOME | all existing Devin auth directories |

Supported canonical models:

- `gpt-5.5-medium`
- `gpt-5.6-sol`
- `gpt-5.6-terra`
- `gpt-5.6-luna`

Cursor supports all four models above. Devin supports `gpt-5.5-medium`
(account-default legacy behavior, matching the native adapter) and
`gpt-5.6-sol` (`gpt-5-6-sol-medium`) only. All other harnesses, models, and
auth strategies fail resolution. There is no model or API-key fallback.

```python
from obench.harbor_profiles import resolve_harbor_profile

profile = resolve_harbor_profile("pi", "gpt-5.6-sol")
agent_config_data = profile.agent_config(
    auth_json_path="/private/job/auth.json",
    auth_return_path="/private/job/auth-return.json",
)
```

`agent_config_data` is accepted by
`harbor.models.trial.config.AgentConfig.model_validate`. The resolver does not
import Harbor, read credential contents, or expose scheduling options.

## OAuth job contract

OAuth support is required for local laptop and Mini jobs. Each benchmark host
authenticates independently; do not copy auth files between hosts.

The job integration owns this complete lifecycle:

1. Select the first existing `profile.auth.source_candidates` entry.
2. Acquire `obench.auth_persist.auth_file_lease(master)` before staging.
3. Create one private mode-`0700` job directory.
4. Call `lease.stage(auth_json_path)` to create the mode-`0600` input.
5. Leave `auth_return_path` absent for the first trial.
6. Use `profile.agent_config(...)` for every trial sharing this credential.
7. Keep the lease held while Harbor runs all trials.
8. After Harbor finishes, call `lease.persist(auth_return_path)` once.
9. Remove both staged files and release the lease.

Every profile emits `n_concurrent = 1` and a stable per-harness
`concurrency_group = "openbench-oauth-<harness>"`. Harbor enforces this
single-credential execution lane. Trial count, retries, and global concurrency
remain Harbor job concerns.

After each trial, the wrapper:

1. atomically captures remote `auth.json` into `auth_return_path`;
2. retains that return file for final host CAS persist-back;
3. atomically refreshes `auth_json_path` from the captured rotation;
4. deletes the isolated remote credential home.

The next sequential trial therefore uploads the latest rotation. A failed
capture fails the agent phase and does not replace the previous valid return
file. The host lease and final schema/identity validation remain authoritative.

## Read-only subscription contract

Cursor and Devin do not use the OAuth rotation path above. Their native
adapters treat local subscription login as read-only, so `job-run` preserves
that behavior:

1. Preflight exact Harbor package provenance before reading auth state or
   starting model traffic.
2. Create a private mode-`0700` temporary directory and mode-`0600` archive.
3. Cursor includes only Linux `auth.json`, or only `authInfo` from the macOS
   `cli-config.json`. Adjacent preferences and permission settings are omitted.
4. Devin includes only `.devin`, `.config/devin`, and
   `.local/share/devin`. Symlinks and special files fail staging.
5. Pass only the private archive path through Harbor's host env template.
6. Extract into an isolated remote HOME, run the CLI, then delete remote and
   host staging.

There is no auth-return path, persist-back, API-key fallback, user
`AGENTS.md`, `.agents`, skill, or global behavior import for either profile.

## ATIF

Codex and OpenCode retain Harbor's built-in ATIF conversion.

Harbor `0.20.0` Pi reports aggregate usage but does not declare
`SUPPORTS_ATIF`. `OpenBenchPiOAuth` declares ATIF support and converts Pi JSONL
with OpenBench's existing tested `convert_pi` converter. It writes
`logs_dir/trajectory.json`, validates ATIF v1.7, stamps the pinned CLI/model
identity, and fills Harbor context token/cost totals from the same trajectory.

OpenCode resume is explicitly disabled. Its OAuth credential state and session
state share release-sensitive XDG surfaces, and the profile deletes those
isolated paths after each single-step OpenBench task.

Cursor runs with the documented `stream-json` output. The converter accepts
only source user, assistant, tool-call, observation, usage, session, and
successful terminal events. Missing JSON, a failed/missing terminal event, or
no attributable agent action fails the profile instead of creating a
trajectory.

Devin writes its native `--export` JSON outside `/app`. OpenBench preserves its
source steps, stamps the pinned harness identity, upgrades the schema marker to
ATIF v1.7, and validates the complete trajectory. A missing, empty, or invalid
export fails the profile. Neither profile derives ATIF from human-readable
stdout.

## Counting proxy

| Harness | OAuth proxy status | Configuration |
|---|---|---|
| Codex | supported | full cell URL through Harbor Codex `OPENAI_BASE_URL` / `openai_base_url`; route `codex/backend-api/codex` |
| Pi | supported | full cell URL written to isolated `models.json` as `openai-codex.baseUrl`; route `codex/backend-api` |
| OpenCode | unsupported | the stock OpenBench OAuth adapter does not prove an equivalent base-URL override |
| Cursor | unsupported | Cursor subscription inference uses its private service protocol |
| Devin | unsupported | Devin subscription inference remains behind Cognition's service boundary |

Passing a proxy URL to OpenCode, Cursor, or Devin fails closed. Profiles accept
only absolute HTTP(S) proxy URLs and never construct cell tokens themselves.

`obench harbor job-run` creates a separate per-trial metering session inside
the Codex and Pi agents. The strict Harbor importer requires a sealed,
hash-chain-verified ledger. Exact call and token reconciliation with ATIF earns
the `proxy-verified` label. A structurally valid mismatch preserves both values
but is excluded from usage rankings. OpenCode publishes its available ATIF
usage as `Harbor-reported`; it cannot claim proxy verification.

## Pinned Harbor API evidence

Compatibility is grounded in Harbor package `0.20.0`, commit
`72bc40b1e58b47a9cc6e0f14c29aced3a9e53767`:

- `harbor.agents.factory.AgentFactory` accepts a custom `module:Class` import.
- `harbor.models.trial.config.AgentConfig` owns `model_name`, `kwargs`, `env`,
  `n_concurrent`, and `concurrency_group`.
- `BaseInstalledAgent` accepts `version`, declarative CLI flags, `config`, and
  `extra_env`.
- Installed Codex accepts `reasoning_effort` and native config.
- Installed Pi accepts `thinking` but lacks ATIF conversion and auth staging.
- Installed OpenCode accepts `variant`, supplies ATIF conversion, and lacks
  OAuth auth-file staging.

Before credential staging or model traffic, `job-run` checks both
`harbor --version` and the package interpreter recorded in the executable
shebang. Harbor's installed package metadata must report the exact commit
above and `is_editable=false`. A wheel install without VCS provenance, an
editable checkout, a different commit, or an ambiguous shebang fails closed.

The focused offline proof resolves every profile and tests the custom
source-log converters without importing Harbor. Release proof must additionally
instantiate both custom profiles through the pinned `AgentFactory` and run
native Harbor Docker jobs on the benchmark host.

## Current boundary

- `obench.harbor_results` maps the Codex, Pi, and OpenCode profile imports to
  stable harness names. Cursor and Devin now provide execution and validated
  ATIF; their strict result-import aliases are integrated separately.
- Codex and Pi result import requires sealed proxy evidence. Exact
  reconciliation is proxy-verified; valid mismatches remain publishable but
  usage-ranking-ineligible. Missing, incomplete, malformed, unsealed, or
  tampered evidence fails closed.
- OpenCode OAuth counting-proxy routing remains unsupported until its exact
  subscription endpoint/base-URL behavior is source-proven and tested; its ATIF
  usage remains publishable as Harbor-reported.
- Cursor and Devin are non-proxy profiles. Their harness-reported usage must not
  be presented as independently metered.
