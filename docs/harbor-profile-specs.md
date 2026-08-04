# Harbor suite profile specs

Suite arms name profile files under `.openbench/profiles/<id>.toml`. The file's
`id` must equal its filename. Schema version 1 has two kinds; unknown keys,
symlinks, path escapes, duplicate execution identities, and implicit model
fallbacks fail closed.

## Stock profiles

```toml
schema_version = 1
id = "local-codex"
kind = "stock"
harness = "codex"
```

`harness` must be `codex`, `pi`, `opencode`, `cursor`, or `devin`. Compilation
delegates to `obench.harbor_profiles.resolve_harbor_profile`, which remains the
source of truth for the exact agent import, CLI version, model mapping, OAuth or
subscription staging, concurrency lane, and counting-proxy policy. Stock files
cannot override those fields and contain no credential paths.

## Custom Harbor agents

```toml
schema_version = 1
id = "acme-agent"
kind = "custom"
import_path = "acme.harbor.agent:AcmeAgent"
version = "2.4.1"
extra_allowed_hosts = ["api.acme.example"]
concurrency_group = "acme-api"
concurrency_limit = 2

[models]
"gpt-5.6-sol" = "openai/gpt-5.6-sol-2026-08-01"
"gpt-5.6-terra" = "openai/gpt-5.6-terra-2026-08-01"

[env]
ACME_API_KEY = "${ACME_API_KEY}"
ACME_BASE_URL = "${ACME_BASE_URL}"

[kwargs]
mode = "strict"
temperature = 0
features = ["tools", "atif"]
```

`import_path` is an exact Python `module:Class` path and `version` is an exact
semantic version. Every supported canonical model has one explicit Harbor
model mapping; two canonical models cannot collapse to the same Harbor model.
There is no fallback for an unlisted model.

`env` is optional, but every value must be exactly `${HOST_ENV}`. Literal
values and credential paths are rejected. `kwargs` is optional and must be
finite JSON-safe data; `version` and sensitive key names are rejected there.
`extra_allowed_hosts` is required even when empty and accepts exact lowercase
DNS or IP hosts, without schemes, ports, or wildcards. Concurrency fields are
optional but must appear together.

Old OpenBench candidate manifests are not accepted or translated. A custom
profile is a native Harbor agent contract, and that agent owns its environment
variable semantics.

## Load and compile

```python
from obench.profile_spec import compile_profile, load_profile

profile = load_profile(".openbench/profiles/acme-agent.toml")
agent = compile_profile(profile, "gpt-5.6-sol")
```

`compile_profile` is pure and returns `obench.harbor_job.AgentProfile`. It does
not import Harbor, read host environment variables, resolve credentials, or
execute a benchmark. `load_profile_registry(project_root)` loads the complete
directory and rejects duplicate identities.
