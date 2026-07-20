# Versioned packs (tasks + harness manifests)

OpenBench packs are versioned, installable-by-name directories —
the lightweight analogue of Prime Intellect’s Environments Hub
(`owner/env@version`), adapted to OpenBench’s **stdlib-only** contract.
There is **no** custom package server and **no** new Python dependencies.

Two pack kinds share one identity scheme and install layout:

| `kind` | Contents | Typical use |
|---|---|---|
| **`tasks`** (default) | Task dirs (`instruction.md` + `checker.sh` + workspace) | Shared eval suites |
| **`harness`** | Candidate manifest TOMLs (`kind = "manifest"` / `config-variant`) | BYO harness distribution |

## Identity

Packs are addressed as:

```
org/name@version
```

Examples: `acme/smoke@1.0.0`, `openbench/aider@1.0.0`.

Omitting `@version` on install takes the version from the source `pack.toml`.
If `@version` is present, it must match `pack.toml`.

## Layout after install

```
.openbench/packs/<org>/<name>/<version>/
  pack.toml
  pack_source.json          # install provenance + digests
  …                         # tasks/ or *.toml manifests
```

### Task packs

```bash
obench validate --tasks-dir .openbench/packs/acme/smoke/1.0.0
obench run --tasks-dir .openbench/packs/acme/smoke/1.0.0 --harness null …
```

Or set `tasks_dir` in `.openbench/openbench.toml`.

### Harness packs

`--candidate` accepts a filesystem path **or** an installed harness-pack ref:

```bash
obench doctor --candidate openbench/aider@1.0.0 --model deepseek-v4-flash
obench gate openbench/aider@1.0.0 --model deepseek-v4-flash
obench run --candidate openbench/aider@1.0.0 --model deepseek-v4-flash …
```

Ref grammar: `org/name[@version][:manifest-stem]`.

- Omit `@version` → latest installed version under `.openbench/packs/`.
- Omit `:manifest` when the pack has exactly one manifest; required when there
  are several (e.g. `acme/tools@1.0.0:aider`).

`doctor` / `gate` / `run` resolve the ref to the installed `.toml` and load it
through the same candidate path as a local file. Manifest `spec_sha256` is
recorded in `pack_source.json` at install time (same digest candidates already
stamp into `candidate_provenance`).

## `pack.toml`

### Task pack

```toml
org = "acme"
name = "smoke"
version = "1.0.0"
kind = "tasks"   # optional; default
description = "Small polarity-checked smoke tasks"
license = "Apache-2.0"

# Optional. Omit to auto-discover child dirs that look like tasks
# (have instruction.md or checker.sh).
tasks = ["make-it-run", "fix-failing-test"]
```

### Harness pack

```toml
org = "acme"
name = "aider"
version = "1.0.0"
kind = "harness"
description = "BYO Aider candidate manifest"
license = "Apache-2.0"

# Optional. Omit to auto-discover *.toml next to pack.toml (except pack.toml).
manifests = ["aider.toml"]
```

## Sources (`--from`)

| Kind | Example | Provenance recorded |
|---|---|---|
| **Local directory** | `--from ./my-pack` | `path` + tree `content_sha256` |
| **Git repo** | `--from git+https://host/repo.git@main#packs/smoke` or a local repo with `--git-ref` / `--git-subdir` | `resolved_sha`, `ref`, `subdir`, tree hash |
| **HTTPS archive** | `--from https://example.com/pack.zip` (also `.tar.gz` / `.tgz`) | `url` + `archive_sha256` + tree hash |

Git exports reuse the same `git archive` staging path as git-mode task
workspaces (`obench/workspace.py`): no `.git` in the installed tree, source
repo never mutated.

Local directories are copied as-is. Pass `--git-ref` and/or `--git-subdir` to
force `git archive` (and a resolved SHA) even for a local checkout.

## CLI

```bash
# Scaffold pack.toml (+ README)
obench pack init --org acme --name smoke --version 1.0.0
obench pack init --org acme --name aider --version 1.0.0 --kind harness

# Install by name from a local pack directory
obench pack install acme/smoke@1.0.0 --from ./path/to/pack

# List installs under .openbench/packs/
obench pack list
obench pack list --json

# Recompute digests vs pack_source.json (task content digests or manifest spec_sha256)
obench pack verify acme/smoke@1.0.0
obench pack verify   # every installed pack

# Maintainer: upsert docs/packs.json + regenerate docs/index.html Packs section
obench pack publish-index --from ./path/to/pack --site-dir docs \
  --source-url data/packs/my-pack
```

Useful flags:

- `--packs-dir` — override install root (default: `.openbench/packs`)
- `--force` — replace an existing install of the same version
- `--git-ref` / `--git-subdir` — pin / select a pack inside a git tree

## Install-time checks

### Task packs

Admission-style **structure** checks for every task:

- **Hard fail:** missing `instruction.md` / `checker.sh`, missing workspace
  (`workspace/` xor `workspace.toml`), workspace conflicts.
- **Warn only:** missing `solution/` or `PROVENANCE.md` (needed for polarity /
  public admission, but private or incomplete packs may still install).

`pack_source.json` records the resolved source plus per-task
`task_content_digest` values (publish digest **scheme 2**: instruction,
checker, workspace / `workspace.toml`, and `checker_data/`).

### Harness packs

Each listed manifest is loaded through `obench.candidates.load_candidate`
(schema / policy-field validation). Hard fail on load errors.

`pack_source.json` records `manifest_digests` / `spec_sha256` (file SHA-256 per
manifest, identical to candidate provenance `spec_sha256`).

`obench pack verify` recomputes digests and fails on mismatch.

## Static pack index (`docs/packs.json`)

Mirrors `releases.json` / `community.json`: a checked-in list of known packs
for the GitHub Pages site. Each entry has `id` (`org/name`), `latest`, `kind`,
`description`, `source`, and `content_sha256` (tree hash of the pack payload,
excluding `pack_source.json`).

The site index (`docs/index.html`) renders a **Packs** section via the same
`_site_index` path as community results. Maintainers refresh it with
`obench pack publish-index`.

Seeded examples in this repo:

| Pack | Kind | Path |
|---|---|---|
| `openbench/core-smoke@1.0.0` | tasks | `data/packs/openbench-core-smoke/` |
| `openbench/aider@1.0.0` | harness | `data/packs/openbench-aider/` |

```bash
obench pack install openbench/core-smoke@1.0.0 --from data/packs/openbench-core-smoke
obench pack install openbench/aider@1.0.0 --from data/packs/openbench-aider
```

## What this is not

- Not a hosted hub or wheel index (no `pip install` of packs).
- Not a substitute for `obench validate` polarity on task packs — run validate
  against the installed pack root before claiming results.
- Not a live admission certificate for harness packs — still run
  `obench gate … --live` before publishing comparison claims.
