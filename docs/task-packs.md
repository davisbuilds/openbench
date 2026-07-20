# Versioned task packs

OpenBench task packs are versioned, installable-by-name directories of tasks —
the lightweight analogue of Prime Intellect’s Environments Hub
(`owner/env@version`), adapted to OpenBench’s **stdlib-only**,
files-plus-shell-checker contract. There is **no** custom package server and
**no** new Python dependencies.

## Identity

Packs are addressed as:

```
org/name@version
```

Examples: `acme/smoke@1.0.0`, `openbench/tier-a@0.2.1`.

Omitting `@version` on install takes the version from the source `pack.toml`.
If `@version` is present, it must match `pack.toml`.

## Layout after install

```
.openbench/packs/<org>/<name>/<version>/
  pack.toml
  pack_source.json          # install provenance + per-task digests
  <task-a>/
  <task-b>/
  …
```

Point the runner / validator at the installed root:

```bash
obench validate --tasks-dir .openbench/packs/acme/smoke/1.0.0
obench run --tasks-dir .openbench/packs/acme/smoke/1.0.0 --harness null …
```

Or set `tasks_dir` in `.openbench/openbench.toml`.

## `pack.toml`

```toml
org = "acme"
name = "smoke"
version = "1.0.0"
description = "Small polarity-checked smoke tasks"
license = "Apache-2.0"

# Optional. Omit to auto-discover child dirs that look like tasks
# (have instruction.md or checker.sh).
tasks = ["make-it-run", "fix-failing-test"]
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
# Scaffold pack.toml (+ README) in the current directory
obench pack init --org acme --name smoke --version 1.0.0

# Install by name from a local pack directory
obench pack install acme/smoke@1.0.0 --from ./path/to/pack

# List installs under .openbench/packs/
obench pack list
obench pack list --json

# Recompute scheme-2 task content digests vs pack_source.json
obench pack verify acme/smoke@1.0.0
obench pack verify   # every installed pack
```

Useful flags:

- `--packs-dir` — override install root (default: `.openbench/packs`)
- `--force` — replace an existing install of the same version
- `--git-ref` / `--git-subdir` — pin / select a pack inside a git tree

## Install-time checks

On install, OpenBench runs admission-style **structure** checks for every task:

- **Hard fail:** missing `instruction.md` / `checker.sh`, missing workspace
  (`workspace/` xor `workspace.toml`), workspace conflicts.
- **Warn only:** missing `solution/` or `PROVENANCE.md` (needed for polarity /
  public admission, but private or incomplete packs may still install).

`pack_source.json` records the resolved source plus per-task
`task_content_digest` values (publish digest **scheme 2**: instruction,
checker, workspace / `workspace.toml`, and `checker_data/`).
`obench pack verify` recomputes those digests and fails on mismatch.

## What this is not

- Not a hosted hub or wheel index (no `pip install` of packs).
- Not harness-manifest packaging yet (roadmap follow-up).
- Not a substitute for `obench validate` polarity — run validate against the
  installed pack root before claiming results.
