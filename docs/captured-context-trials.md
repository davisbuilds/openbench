# Captured-context trials

Config variants can opt into a stricter native trial path with
`captured_context = true`. It is intended for comparisons where a harness must
receive a captured set of settings and skills, with complete local evidence.
Existing stock and config-variant routes retain their default behavior.

This path uses the explicit native compatibility runner (`obench legacy run`
or `obench matrix`), with `exec_mode = "local"`. Canonical suites still use
`obench run` and Harbor. Captured variants currently reject Docker execution
and counting-proxy routing before dispatch; their usage comes from the adapter.

## Candidate contract

```toml
kind = "config-variant"
name = "captured-codex"
base_adapter = "codex"
captured_context = true
config_dir = "capture"
config_files = [
  { source = "codex.toml", destination = "codex/config.toml" },
  { source = "skill.md", destination = "home/.agents/skills/example/SKILL.md" },
]

[env]
CODEX_HOME = "{config_dir}/codex"

[[auth_files]]
source = "~/.openbench/lanes/example-codex/auth.json"
destination = "codex/auth.json"
```

The example names a **separately authenticated lane**, which must be provisioned
by the operator. It does not authorize copying an everyday login. Auth files
are staged separately from configuration, under the existing credential lease;
they are never included in configuration hashes. Persist-back remains off
unless `persist_auth = true` is explicitly declared.

At candidate load, every enumerated config file is captured as bytes. Later
source edits do not affect that loaded candidate. Configuration hashes and the
manifest digest identify the treatment. Separate CLI invocations reload the
files: keep capture roots immutable and verify their digests before dispatch.
Each dispatch materializes those bytes
into a fresh directory. Symlinks resolving outside the source root are rejected;
internal source symlinks are materialized as bytes. Enumerate every intended
asset, rather than passing a live directory tree.

Captured candidates require direct adapter `env_override` and `replace_env`
support; there is no process-global environment fallback. Only basic process
variables (PATH, locale, temporary-directory variables), explicitly named
`pass_env`, and declared `[env]` values reach the adapter. `inherit_env = true`
is rejected. Authentication secrets belong in declared `auth_files` or
`pass_env`, never literal `[env]` values or captured configuration files.
Configuration root variables must point inside `{config_dir}`.

HOME is always adapter-owned. Put home-scoped files under the `home/`
destination prefix, as above. The candidate supplies that captured tree through
`OPENBENCH_CAPTURED_HOME`; the adapter copies it into its own fresh HOME while
preserving the explicit CODEX_HOME or CLAUDE_CONFIG_DIR. Symlinks and special
files in the staged home are rejected. `{home}` in a template names the staging
home, not the eventual child HOME; prefer relative discovery paths. Declaring
HOME directly is rejected.

## Claude subscription route

```toml
kind = "config-variant"
name = "captured-claude"
base_adapter = "claude"
captured_context = true
config_dir = "capture"
config_files = [
  { source = "settings.json", destination = "claude/settings.json" },
  { source = "skill.md", destination = "claude/skills/example/SKILL.md" },
]
pass_env = ["CLAUDE_CODE_OAUTH_TOKEN"]

[env]
CLAUDE_CONFIG_DIR = "{config_dir}/claude"
OPENBENCH_CLAUDE_AUTH_MODE = "subscription"
```

The subscription route requires an explicitly supplied, separately provisioned
`CLAUDE_CODE_OAUTH_TOKEN`. Missing lane configuration fails before model
dispatch; the adapter does not fall back to a daily login or API key. This route
uses stream JSON with tool events and keeps skill discovery enabled. Existing
first-party API and vendor routes retain `--bare` and their API-key routing.
Subscription runs reject `apiKeyHelper` and conflicting auth/route environment
settings in captured and project settings; enterprise managed policy remains a
live admission check.
Captured Claude requires this subscription route; API routes with `--bare`
do not supply a skill-enabled treatment. Use a native Claude model present in the adapter's `MODELS` mapping.

The installed CLI's `claude setup-token` and Anthropic's
[authentication documentation](https://code.claude.com/docs/en/authentication)
describe subscription tokens. Actual token provisioning requires a separate
operator action; do not include credentials in manifests, logs, or Git.

## Codex Astra aliases

Native Codex supports `gpt-6-astra` (medium), plus `gpt-6-astra-low`,
`-medium`, `-high`, `-xhigh`, and `-max`. Each resolves to canonical
`gpt-6-astra` with the corresponding explicit reasoning effort and default
service tier. These are adapter mappings, not evidence of account access.
See the [model documentation](https://developers.openai.com/api/docs/models/gpt-6-astra).
Captured Codex currently rejects bridge/open-model routes.

## Local evidence and failure handling

The runner writes an atomic, immutable bundle keyed by run ID and a unique attempt ID
beside local transcripts. It includes trial identity, full raw output, final
assistant text, tool events, parser-error details, and component hashes. The
row's `evidence_sha256` binds the complete bundle. Files use mode 0600 and the
containing directory 0700. Identical repeated persistence is idempotent;
conflicting bytes for the same attempt fail without overwriting it. Automatic
retries keep the stable cell run ID but receive distinct evidence attempt IDs;
all attempt bundles are retained. The text transcript is the latest diagnostic copy.

Rows carry `evidence_attempt_id`, `evidence_required`, `evidence_status`,
`evidence_sha256`, and `evidence_error_code`; raw contents and local evidence paths are not added to
public rows. Evidence remains LOCAL-ONLY and must be reviewed/scrubbed before
sharing. The legacy text transcript is retained for diagnostics.

Captured candidates require evidence. Accept a trial record only when
`evidence_status == "complete"`, its bundle exists and its hash and identity
match the row. Missing, malformed, truncated, or unwritable evidence is
explicitly recorded. Required evidence loss classifies the row as infrastructure
failure while preserving the independent checker's score and success flag;
**checker success alone is not a usable captured trial record**. A partial checker
must emit `SCORE: <float>` and exit nonzero; exit zero always means full success
under the existing checker contract.

The matrix runner's existing retry policy still applies. For a frozen experiment
ledger, set infrastructure retries to zero and stop/reconcile evidence loss
before further dispatch. A diagnostic retry receives its own evidence attempt ID and
must remain a separate observation in the experiment ledger. This feature neither schedules
randomized blocks nor implements an experiment's statistical policy.

## Before a live pilot

Offline tests prove configuration forwarding, environment construction, stream
parsing, and real filesystem persistence through synthetic CLI executables.
They do not prove actual harness discovery, consultation, account access, or
filesystem confinement. Tool events are evidence to inspect, not an automatic
claim that a skill was consulted.

Before any live calls, prepare a concrete bounded smoke receipt with:

- exact pushed code, CLI versions, model/effort, captured inputs and hashes;
- separately authenticated credential lanes and serial launch accounting;
- a sandbox boundary or explicit approval for supervised same-user execution;
- an inert known-present and known-absent discovery/consultation probe;
- a private evidence destination, timeout and process-tree stop procedure.

Get operator approval for authentication and that bounded live execution.
The native Codex invocation requests `workspace-write`; native Claude uses
`--dangerously-skip-permissions` and relies on the separately approved execution
boundary. HOME isolation controls ambient discovery; it does not deny arbitrary reads of
other same-user files. Managed machine policy, project instructions, hooks and
plugins must be inspected through the actual harness path during admission.
Do not run a broader experiment to compensate for an unproven prerequisite.
