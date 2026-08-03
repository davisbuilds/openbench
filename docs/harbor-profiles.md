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

Supported canonical models:

- `gpt-5.5-medium`
- `gpt-5.6-sol`
- `gpt-5.6-terra`
- `gpt-5.6-luna`

All other harnesses, models, and auth strategies fail resolution. There is no
model fallback or API-key fallback.

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

## Counting proxy

| Harness | OAuth proxy status | Configuration |
|---|---|---|
| Codex | supported | full cell URL through Harbor Codex `OPENAI_BASE_URL` / `openai_base_url`; route `codex/backend-api/codex` |
| Pi | supported | full cell URL written to isolated `models.json` as `openai-codex.baseUrl`; route `codex/backend-api` |
| OpenCode | unsupported | the stock OpenBench OAuth adapter does not prove an equivalent base-URL override |

Passing a proxy URL to the OpenCode profile fails closed. Profiles accept only
absolute HTTP(S) proxy URLs and never construct cell tokens themselves.

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

The focused offline proof instantiates every resolved profile through the pinned
`AgentFactory`. No Docker, model, API, SSH, Mini, benchmark, or publish action is
part of this proof.

## Integration blockers

- `obench.harbor_results.HARBOR_AGENT_SEMANTIC_NAME_ALIASES` currently knows
  only the older Codex OAuth import path. Before importing results from these
  profiles, the Harbor-results owner must map all three profile import paths to
  `codex`, `pi`, and `opencode` respectively. This file is outside the profile
  owner's allowed write scope.
- OpenCode OAuth counting-proxy routing remains unsupported until its exact
  subscription endpoint/base-URL behavior is source-proven and tested.
- Live OAuth rotation and CLI behavior remain operator-run integration proof;
  this implementation was intentionally verified without credentials or model
  calls.
