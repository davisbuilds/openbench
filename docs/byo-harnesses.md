# Bring your own harness

Pass one or more candidate files with `--candidate`; each file's `name` becomes
the independent results group label (and therefore part of `run_id`). Candidates
may be mixed with stock `--harness` names.

## Config variant

```toml
kind = "config-variant"
name = "codex-v2"
base_adapter = "codex"
config_dir = "../../ablation/codex-home-v2"
config_files = ["config.toml", "pi-style-instructions.md"]
[env]
CODEX_HOME = "{config_dir}"
[[auth_files]]
source = "~/.codex/auth.json"
destination = "auth.json"
```

Files are copied to a disposable directory. `{config_dir}`, `{workspace}`, and
`{model}` may be used in environment values. The base adapter retains model
mapping, output parsing, version capture, and proxy behavior. The checked-in
V2 declaration is `ablation/codex-home-v2/candidate.toml`.

## Generic manifest

```toml
kind = "manifest"
name = "my-cli"
isolate_home = true
command = ["my-cli", "run", "--model", "{model}", "--workspace", "{workspace}", "{prompt}"]
version_command = ["my-cli", "--version"]
unset_env = ["MY_CLI_CONFIG"]
base_url_env = "MY_CLI_BASE_URL"
proxy_route = "chat/vendor/v1"

[models]
gpt-5.5-medium = "gpt-5.5"
[env]
MY_CLI_HOME = "{home}/.my-cli"
[[auth_files]]
source = "~/.my-cli/auth.json"
destination = ".my-cli/auth.json"
```

Commands are argv arrays and never run through a shell. Supported placeholders
are `{prompt}`, `{workspace}`, `{model}`, and `{home}`. Auth files are copied to
the disposable home; missing files return `SETUP-NEEDED`. `base_url_env` and
`proxy_route` opt the CLI into the counting proxy. Generic output is retained as
a transcript; token fields remain unknown unless independently metered by the
proxy. See `bench/examples/pi-harness.toml` for a complete stock-equivalent
manifest.

Both kinds record their spec digest, configuration digests, command/model data,
auth paths, and environment variable names in `candidate_provenance`. Values of
environment variables and auth contents are deliberately excluded.
