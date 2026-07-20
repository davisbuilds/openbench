# Harbor export bridge

Exporter from OpenBench tasks to [Harbor](https://www.harborframework.com/)
task directories. Companies can run OpenBench suites on Harbor’s cloud sandboxes
while OpenBench stays the comparison / stats / auth layer. The reverse direction
(`obench import harbor`) is documented in [`docs/harbor-import.md`](harbor-import.md).

```bash
obench export harbor --task all --out /tmp/harbor-tasks
obench export harbor --task make-it-run,fix-failing-test --out /tmp/harbor-tasks
```

Requires no Harbor install to export or to run the local round-trip polarity
harness. Harbor itself is optional for `harbor run` smoke checks.

## What gets exported

For each OpenBench task `tasks/<name>/` the exporter writes a Harbor task dir:

```
<out>/<name>/
  instruction.md          # copied from OpenBench
  task.toml               # Harbor schema_version 1.3 metadata + config
  environment/
    Dockerfile            # python:3.11-slim, WORKDIR /app, COPY app/
    app/                  # materialized OpenBench workspace
  tests/
    test.sh               # wraps checker.sh → /logs/verifier/reward.txt
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
| (none / provenance) | `[metadata].origin = "openbench"` |
| (none) | `[metadata].difficulty = "unknown"` |
| tags (synthetic) | `[metadata].tags` / `[task].keywords` include `openbench` |
| `workspace/` or staged `workspace.toml` | `environment/app/` + `COPY app/ /app/` |
| git `resolved_sha` | `[metadata].openbench_workspace_resolved_sha` |
| `checker.sh` + exit 0 | `tests/test.sh` writes `1.0` to reward file |
| `SCORE: <float>` on nonzero exit | same float written to reward file |
| nonzero exit, no SCORE | reward `0.0` |
| `checker_data/` | `tests/checker_data/` (`TASK_DIR` = `tests/`) |
| `solution/` files | `solution/` + generated `solve.sh` oracle overlay |

## Verifier reward path

Harbor expects `/logs/verifier/reward.txt` (or `reward.json`). Generated
`tests/test.sh` resolves the log directory as:

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
| **Network policy** | Default `[environment].network_mode = "no-network"` (OpenBench checkers are offline). Override with `--network-mode public` when a task needs egress at build/run time. |
| **Partial credit** | Mapped into Harbor’s scalar reward (`reward.txt`). Untouched partial-credit tasks can therefore export a baseline reward > 0 (e.g. `add-feature` ≈ 0.4) while still failing polarity (reward < 1 until the oracle). Harbor leaderboards that threshold at 1.0 stay compatible; keep using `obench report` when you need OpenBench’s binary-vs-partial framing. |
| **Checker visibility** | Checker + `checker_data` live under `tests/`, which Harbor copies to `/tests` at verify time (not part of the agent’s starting workdir). Separate verifier images (`[verifier.environment]`) are **not** emitted by default — enable manually if you need a harder anti-cheat boundary. |
| **Base image** | `python:3.11-slim` (bash + python3). Harbor’s hello-world example uses `ubuntu:24.04`; override with `--base-image` if you need apt-heavy tooling. |
| **Schema field name** | Live docs / Terminal-Bench challenges use `schema_version = "1.3"`. Some older Harbor examples still show legacy `version = "1.0"`. This exporter follows **1.3**. |
| **Auth / metering** | Not bridged. Harbor agents use Harbor’s auth story; OpenBench subscription/OAuth + counting-proxy metering stay on the OpenBench path. |
| **Multi-step Harbor tasks** | Not generated. |

## Format verification sources (Jul 2026)

- Task layout, `instruction.md`, `task.toml`, `environment/`, `tests/test.sh`,
  `solution/solve.sh`, reward files:
  https://www.harborframework.com/docs/tasks
- License Apache-2.0:
  https://github.com/harbor-framework/harbor (and `pyproject.toml` `license = "Apache-2.0"`)
- Current schema default `1.3` / network_mode:
  https://github.com/harbor-framework/harbor/blob/main/CHANGELOG.md
- Example challenge using `schema_version = "1.3"`:
  https://github.com/harbor-framework/terminal-bench-challenges/blob/main/inference-engine-codegolf/task.toml

**Discrepancy vs older summaries:** Harbor’s checked-in `examples/tasks/hello-world/task.toml`
still uses legacy top-level `version = "1.0"` and flat `[metadata]` tags; the
published docs and newer TB tasks use nested `[task]` + `schema_version = "1.3"`.
OpenBench follows the docs / 1.3 shape.
