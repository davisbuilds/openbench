# OpenBench adapter contract

OpenBench runs each candidate against a disposable copy of a task workspace. A
candidate may be a Python adapter, a named config variant of one, or a generic
`harness.toml` manifest. In every case the checker—not the harness—decides task
success.

## Invocation

A Python adapter is `bench/adapters/<name>.py` and exposes:

```python
NAME = "name"
MODELS = {"canonical-model": "cli-model"}
def run(instruction: str, workdir: str, model: str, timeout_s: int) -> dict: ...
def version() -> str | None: ...  # optional
```

`run` must be headless, enforce `timeout_s`, and return at least
`completed`, `error`, `output_tail`, `tokens`, `turns`, and `cmd`. It may also
return `full_output`, normalized token fields, `usage_raw`, and `token_basis`.
`completed` means the CLI completed successfully; it does not mean the task
passed.

The runner copies `tasks/<task>/workspace/` to a temporary `workdir`, reads
`instruction.md`, invokes the candidate, then runs `checker.sh` in `workdir`
with `TASK_DIR` pointing to the source task. Adapters must set both their child
process cwd and any CLI workspace flag to `workdir` when the CLI has one.
Task-supplied config must not escape this disposable boundary or silently load
as executable harness configuration.

## Model pinning and versions

The runner passes a canonical model name. Each adapter validates and translates
it to the CLI's model identifier and explicitly selects reasoning effort where
the CLI permits it. An unsupported pin returns a non-completed result; it must
not fall back silently. Any unavoidable limitation (for example a CLI with no
effort flag) must be documented.

`version()` is a cheap, defensive `--version` probe and should include the
resolved executable path when available. Local runs cache it once per candidate.
Docker rows use `/etc/openbench-cli-versions.json` from the image and record
`harness_version_source="container"`. Candidate provenance also records the
candidate kind, source specification, base adapter, config/manifest digest, and
environment variable names; secret values are never provenance.

## Execution modes

- **local** imports and runs the adapter on the host. Adapters isolate mutable
  CLI homes/config and copy only the minimum required auth material.
- **docker** starts one disposable container per cell. `/work` is writable;
  adapters, entrypoint, instruction, candidate spec/config, and auth staging are
  read-only mounts. The adapter still runs unchanged inside the container.
  Host auth is never baked into the image. If Docker is unavailable, the runner
  may fall back to local unless `--no-docker-fallback` is set.

Both modes must return which lane actually ran. Timeouts and adapter exceptions
become failed rows rather than terminating the matrix.

## Authentication and configuration

Adapters may read existing credentials, but must not modify the user's real
configuration. Prefer a fresh temporary HOME/config directory containing only
required auth files. Config variants stage declared config files in a fresh
directory and overlay declared environment variables for the base adapter.
Manifest auth mappings similarly copy only explicitly listed source files to a
disposable home. Generic manifests receive a minimal environment by default;
`pass_env` explicitly forwards named host variables, while `inherit_env=true`
is a compatibility escape hatch that should be avoided for untrusted harnesses.
Specs contain paths and environment variable names, never credential contents.

## Token and output fields

Adapters report fields when the CLI exposes them; unknown values are `None`:

- `tokens_input_uncached`
- `tokens_cache_read`
- `tokens_cache_write`
- `tokens_output` (reasoning-inclusive when the provider reports it that way)
- `tokens_reasoning`
- `tokens` (legacy fresh-token scalar, normally uncached input + output)
- `tokens_fresh`, derived by the runner when possible
- `turns`, `usage_raw`, and `token_basis`

`token_basis` distinguishes vendor-split, harness-reported, proxy-measured, and
estimated accounting. `output_tail` is bounded; optional `full_output` is stored
only in local transcripts and must be scrubbed before sharing. The runner also
captures checker output and bounded workspace evidence, scrubbing both before
persistence.

## Counting proxy routing

With `--proxy`, the runner creates a per-cell token and injects
`OPENBENCH_PROXY`, `OPENBENCH_PROXY_BASE_URL`, and
`OPENBENCH_PROXY_CELL_TOKEN`. Supported adapters replace their provider base URL
with a path under:

```text
/cell/<token>/codex/...
/cell/<token>/chat/<vendor>/...
/cell/<token>/anthropic/<vendor>/...
```

`bench/proxy.py` strips that prefix, forwards to the configured upstream, and
writes scrubbed usage rows to the cell ledger. A generic manifest may declare a
base-URL environment variable and proxy route; the runner supplies its per-cell
URL. A candidate that cannot redirect model traffic must be marked unsupported
for proxy metering rather than claiming measured usage. Cursor's private HTTP/2
protocol and Devin's cloud-side inference are current examples.

## Candidate tiers

A config variant gives a stable group label to a stock adapter plus staged
configuration and environment overlays. Its base adapter owns CLI behavior,
auth, parsing, model mapping, and proxy support.

A generic manifest describes an arbitrary CLI without Python: argv template,
environment, auth-file mappings, version command, model mapping, and optional
base-URL proxy routing. Scalar placeholders are expanded per cell: `{prompt}`, `{workspace}`, `{model}`,
and `{home}`. A whole argv element equal to `{workspace_files}` expands sorted
relative paths selected by declared `workspace_file_globs`, for CLIs that need
editable files passed positionally. Templates are arrays, not shell strings, so
no shell parsing or interpolation occurs.

See `bench/ADAPTER_SPEC.md` for the legacy Python API and the examples in
`docs/byo-harnesses.md` for the declarative schemas.
