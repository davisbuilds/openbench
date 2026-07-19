# Bring your own harness

Pass one or more candidate files with `--candidate`; each file's `name` becomes
the independent results group label. Candidate `run_id` values add a short
content digest (`name@digest:...`) so editing a spec/config cannot silently reuse
stale rows. Candidates
may be mixed with stock `--harness` names.

## Config variant

```toml
kind = "config-variant"
name = "codex-v2"
base_adapter = "codex"
config_dir = "../../ablation/codex-home-v2"
config_files = [
  # String entries copy unchanged. Table entries may rename and expand
  # {config_dir}/{workspace}/{model} in text files while staging.
  { source = "candidate-config.toml", destination = "config.toml", template = true },
  "pi-style-instructions.md",
]
[env]
CODEX_HOME = "{config_dir}"
[[auth_files]]
source = "~/.codex/auth.json"
destination = "auth.json"
```

Files are copied to a disposable directory. `{config_dir}`, `{workspace}`, and
`{model}` may be used in environment values and in config entries marked
`template = true`. Config entries may also rename a source with `destination`.
The base adapter retains model mapping, output parsing, version capture, and
proxy behavior. The checked-in V2 declaration is
`ablation/codex-home-v2/candidate.toml`; its staged config is byte-equivalent to
the former `env_override` composer.

A config variant may also select an adapter-supported experimental toggle. The
Codex multi-agent ON arm is checked in at
`experiments/multiagent-toggle/codex-on.toml`:

```toml
kind = "config-variant"
name = "codex-multiagent-on"
base_adapter = "codex"
config_dir = "codex-home"
config_files = ["config.toml"]
[env]
CODEX_HOME = "{config_dir}"
OPENBENCH_CODEX_MULTI_AGENT = "enabled"
```

That marker is consumed by the Codex adapter and changes only its explicit
`multi_agent` feature pin from `--disable` to `--enable`; an inherited host
environment variable cannot turn on the stock arm.

## Generic manifest

```toml
kind = "manifest"
name = "my-cli"
isolate_home = true
command = ["my-cli", "run", "--model", "{model}", "--workspace", "{workspace}",
           "{workspace_files}", "{prompt}"]
workspace_file_globs = ["src/**/*", "*.toml"]
version_command = ["my-cli", "--version"]
# The safe default does not inherit arbitrary host variables. Name only the
# credentials/settings this CLI needs; Docker forwards these without values in argv.
pass_env = ["VENDOR_API_KEY"]
unset_env = ["MY_CLI_CONFIG"]
base_url_env = "MY_CLI_BASE_URL"
proxy_route = "chat/vendor/v1"

[models]
"gpt-5.5-medium" = "gpt-5.5"
[env]
MY_CLI_HOME = "{home}/.my-cli"
[[auth_files]]
source = "~/.my-cli/auth.json"
destination = ".my-cli/auth.json"
```

Commands are argv arrays and never run through a shell. By default the child
environment contains only basic process variables, declared `pass_env` names,
manifest `[env]` values, and runner proxy variables. `inherit_env = true` is an
explicit compatibility escape hatch for stock-equivalence cases; it may expose
unrelated host credentials and should not be used for new manifests.
Supported scalar placeholders are `{prompt}`, `{workspace}`, `{model}`, and
`{home}`. The special whole-argument placeholder `{workspace_files}` expands to
sorted, de-duplicated relative file paths matched by `workspace_file_globs`;
the two must be declared together. Matches are contained within the disposable
workspace. This supports CLIs that require editable files as positional argv
instead of discovering them from their working directory. Auth files are copied to
the disposable home; sources must use home-relative `~/...` paths and missing
files return `SETUP-NEEDED`. `base_url_env` and
`proxy_route` opt the CLI into the counting proxy. `proxy_route` is the path
after `/cell/<token>/` (for example `chat/zai/api/paas/v4`); the CLI must honor
the declared base-URL environment variable. Generic output is retained as a
transcript; token fields remain unknown unless independently metered by the
proxy. See `bench/examples/pi-harness.toml` for a complete invocation-equivalent
manifest (it deliberately does not claim proxy support because Pi's native
adapter routes its subscription endpoint through a generated config file).

In Docker mode the candidate file's directory is mounted read-only, so config
sources must live in that directory tree. Declared auth sources must be under
the user's home directory; they are mounted read-only and copied into the
container's disposable home.

Both kinds record their spec digest, configuration digests, command/model data,
auth paths, environment policy, and environment variable names in
`candidate_provenance`, including the full candidate identity digest. Values of
environment variables and auth contents are deliberately excluded.
