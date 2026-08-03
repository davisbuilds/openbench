# Harbor export bridge

Exporter from OpenBench tasks to [Harbor](https://www.harborframework.com/)
task directories. Companies can run OpenBench suites on Harbor’s cloud sandboxes
while OpenBench stays the comparison / stats / auth layer. The reverse direction
(`obench import harbor`) is documented in [`docs/harbor-import.md`](harbor-import.md).

```bash
obench export harbor --task all --out /tmp/harbor-tasks
obench export harbor --task make-it-run,fix-failing-test --out /tmp/harbor-tasks
```

Codex OAuth tasks require public egress. Export those tasks explicitly:

```bash
obench export harbor \
  --task make-it-run \
  --out /tmp/harbor-oauth-tasks \
  --network-mode public
```

Requires no Harbor install to export or to run the local round-trip polarity
harness. Harbor itself is optional for `harbor run` smoke checks.

## What gets exported

For each OpenBench task `tasks/<name>/` the exporter writes a Harbor task dir:

```
<out>/<name>/
  instruction.md          # copied from OpenBench
  task.toml               # Harbor schema_version 1.4 metadata + config
  environment/
    Dockerfile            # python:3.11-slim, WORKDIR /app, COPY app/
    app/                  # materialized OpenBench workspace
  tests/
    test.sh               # writes scalar reward + machine-readable evidence
    checker.sh            # OpenBench checker (verifier-only context)
    checker_data/         # optional; TASK_DIR points here at verify time
  solution/               # when OpenBench has solution/
    solve.sh              # oracle: overlay solution files onto cwd
    …                     # golden files from OpenBench solution/
```

Git-mode tasks (`workspace.toml`) are staged with `obench.workspace` first; the
resolved commit SHA is recorded in `task.toml` metadata
(`openbench_workspace_resolved_sha`).

Exports never include transcripts, results logs, or auth material.

## Field mapping

| OpenBench | Harbor |
|---|---|
| `instruction.md` | `instruction.md` |
| task directory name | `[task].name = "openbench/<name>"` |
| synthetic package version | `[task].version = "1.0.0"` |
| (none / provenance) | `[metadata].origin = "openbench"` |
| (none) | `[metadata].difficulty = "unknown"` |
| tags (synthetic) | `[metadata].tags` / `[task].keywords` include `openbench` |
| `workspace/` or staged `workspace.toml` | `environment/app/` + `COPY app/ /app/` |
| final `/app` state | task artifact collected under `artifacts/workspace/` |
| git `resolved_sha` | `[metadata].openbench_workspace_resolved_sha` |
| scheme-2 task content digest | `[metadata.openbench_task_content_digest]` and verifier evidence |
| `checker.sh` + exit 0 | `tests/test.sh` writes `1.0` to reward file |
| `SCORE: <float>` on nonzero exit | same float written to reward file |
| nonzero exit, no SCORE | reward `0.0` |
| `checker_data/` | `tests/checker_data/` (`TASK_DIR` = `tests/`) |
| `solution/` files | `solution/` + generated `solve.sh` oracle overlay |

## Verifier reward path

Harbor expects `/logs/verifier/reward.txt` (or `reward.json`). The exporter
retains the scalar `reward.txt` contract and also writes the structured
`openbench-verifier-evidence-v2` record in
`openbench-verifier-evidence.json`. It contains the original checker exit, the
last parseable clamped `SCORE` (or `null`), final reward, whole-second
verifier-wrapper duration, and the same `{scheme = 2, sha256 = ...}` task
content digest recorded in `task.toml`. Generated `tests/test.sh` resolves the
log directory as:

1. `$VERIFIER_LOGS_DIR` if set (local round-trip / tests)
2. else `/logs/verifier` when that directory already exists (Harbor runtime)
3. else `./logs-verifier` under the agent workspace cwd

This keeps the script Harbor-compatible without requiring root or a `/logs`
mount for unit tests.

## Recommended flow

```bash
# 1. Export
obench export harbor --task all --out ./harbor-out

# 2. Local Harbor run (optional; needs `harbor` CLI)
harbor run -p ./harbor-out/make-it-run -a oracle   # polarity smoke via oracle
harbor run -p ./harbor-out/make-it-run -a <agent> -m <model>

# 3. Keep scoring / comparison in OpenBench
obench run --harness pi,opencode --task make-it-run --trials 3
obench report
obench compare ...
```

Private-repo tasks under `.openbench/tasks/` export the same way — pass
`--tasks-dir .openbench/tasks`.

To pull Harbor / Terminal-Bench-format tasks *into* OpenBench (adapters,
metering, report), use [`obench import harbor`](harbor-import.md).

## Known gaps / decisions

| Topic | Choice |
|---|---|
| **Network policy** | Default `[environment].network_mode = "no-network"` (OpenBench checkers are offline). Harbor's Docker enforcement requires Linux nftables `fib` support. Docker Desktop for macOS may lack `CONFIG_NFT_FIB_INET`, in which case Harbor rejects `no-network` and `allowlist`; use a compatible Linux Docker runtime rather than silently changing a comparable run to `public`. Override with `--network-mode public` only when the task contract intentionally permits egress. |
| **Partial credit** | Mapped into Harbor’s scalar reward (`reward.txt`). Untouched partial-credit tasks can therefore export a baseline reward > 0 (e.g. `add-feature` ≈ 0.4) while still failing polarity (reward < 1 until the oracle). Harbor leaderboards that threshold at 1.0 stay compatible; keep using `obench report` when you need OpenBench’s binary-vs-partial framing. |
| **Checker visibility** | Checker + `checker_data` live under `tests/`, which Harbor copies to `/tests` at verify time (not part of the agent’s starting workdir). Separate verifier images (`[verifier.environment]`) are **not** emitted by default — enable manually if you need a harder anti-cheat boundary. |
| **Base image** | `python:3.11-slim` (bash + python3). Harbor’s hello-world example uses `ubuntu:24.04`; override with `--base-image` if you need apt-heavy tooling. |
| **Schema contract** | Pinned Harbor `0.20.0` source defaults tasks and templates to `schema_version = "1.4"`. The exporter emits **1.4**, including `[task].version` and final `/app` artifact collection. OpenBench's importer continues accepting 1.3 tasks. |
| **Auth / metering** | Not bridged. Harbor agents use Harbor’s auth story; OpenBench subscription/OAuth + counting-proxy metering stay on the OpenBench path. |
| **Multi-step Harbor tasks** | Not generated. |

## Format verification sources (Aug 2026)

- Harbor package version `0.20.0`, commit
  [`72bc40b1e58b47a9cc6e0f14c29aced3a9e53767`](https://github.com/harbor-framework/harbor/tree/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767).
- Task schema default, package metadata, and artifact fields:
  [`src/harbor/models/task/config.py`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/src/harbor/models/task/config.py).
- Task layout, verifier reward files, and artifact collection:
  [`docs/content/docs/tasks`](https://github.com/harbor-framework/harbor/tree/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/docs/content/docs/tasks) and
  [`results-and-artifacts.mdx`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/docs/content/docs/run-jobs/results-and-artifacts.mdx).
- macOS Docker nftables limitation:
  [`network-policy.mdx`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/docs/content/docs/tasks/network-policy.mdx#L40-L65).
- License Apache-2.0:
  [`LICENSE`](https://github.com/harbor-framework/harbor/blob/72bc40b1e58b47a9cc6e0f14c29aced3a9e53767/LICENSE).
