# OpenBench setup

This is the shortest path for a first-time visitor to run one benchmark cell and then scale up to the imported Terminal-Bench tier or open-model runs.

## Requirements

- Python 3.11+ (the benchmark code uses only the Python standard library).
- `bash`, `git`, and a Unix-like shell.
- For local harness runs: the target harness CLI installed and already authenticated.
- For Docker isolation: Docker Desktop / Docker Engine, plus a built image (below).
- Hardware: no GPU required. The core and Exercism tasks are light; the Terminal-Bench tier can run for several minutes per cell and benefits from a normal developer laptop/desktop CPU.

## Harness CLIs and auth

OpenBench benchmarks harnesses, so the CLIs are intentionally external dependencies:

| Harness | CLI command | Auth/cost caveat |
|---|---|---|
| `null` | built in | Negative control; no model, no cost. |
| `codex` | `codex` | Uses existing Codex/ChatGPT subscription login for `gpt-5.5-medium`. Open models require the bridge below. |
| `pi` | `pi` | Uses existing Pi auth copied into an isolated temp `HOME`; extensions are disabled. |
| `opencode` | `opencode` | Uses subscription OAuth for frontier runs; first-party API keys for open-model runs. |
| `cursor` | `cursor-agent` | Closed model menu; local/subscription auth only. |
| `devin` | `devin` | Closed model menu; currently treated as flaky in published analysis. |
| `claude` | `claude` | Open-model adapter only. It routes to vendor Anthropic-compatible endpoints using vendor API keys; it deliberately does not use Anthropic subscription/OAuth models. |

Run a no-spend preflight for installed real CLIs and model mapping:

```bash
python3 bench/doctor.py --harness codex,pi,opencode --model gpt-5.5-medium
```

`doctor.py` checks availability/auth/model resolution only; it does not spend tokens. The built-in `null` control has no external CLI/auth and is exercised with `bench/run.py` instead.

## Local provider keys (`~/.openbench/keys.env`)

Open-model runs use first-party provider keys. Keep them outside the repo. The expected names are:

```dotenv
ZAI_API_KEY=
DEEPSEEK_API_KEY=
MOONSHOT_API_KEY=
```

Do not commit this file. Fill the values locally or export the same names in your shell. `bench/openmodel_bridge.sh` also reads this file when present.

## Validate tasks

Before benchmarking, prove every checker is polarized: the untouched workspace must fail and the golden solution must pass.

```bash
python3 validate_tasks.py
```

This validates both `tasks/` and maintainer-curated imported tiers under `tasks-imported/`.

## Run one cell end-to-end

Start with the zero-cost negative control:

```bash
python3 bench/run.py \
  --harness null \
  --task fix-failing-test \
  --trials 1 \
  --results-path /tmp/openbench-smoke.jsonl

python3 bench/report.py --efficiency --results-path /tmp/openbench-smoke.jsonl
```

A real frontier/subscription cell is the same shape, but requires that harness CLI to be logged in:

```bash
python3 bench/run.py \
  --harness pi \
  --task fix-failing-test \
  --model gpt-5.5-medium \
  --trials 1 \
  --results-path /tmp/openbench-pi.jsonl
```

The default result log is `results/results.jsonl`; `results/` and raw `transcripts/` are local-only and gitignored.

## Imported tasks (`--tasks-dir tasks-imported`)

Imported tiers are addressed as `collection/task` and are scored separately from the core tier.

Exercism example:

```bash
python3 bench/run.py \
  --tasks-dir tasks-imported \
  --harness null \
  --task exercism/luhn \
  --results-path /tmp/openbench-exercism-smoke.jsonl
```

Terminal-Bench frontier tier example (use Docker isolation):

```bash
docker build -t openbench-harness:latest bench/docker

python3 bench/run.py \
  --tasks-dir tasks-imported \
  --exec docker \
  --harness pi \
  --task terminal-bench/count-call-stack \
  --model gpt-5.5-medium \
  --trials 1 \
  --results-path /tmp/openbench-tb.jsonl
```

The default Docker image installs the verified Linux-portable CLIs (`codex`, `pi`, `claude`). `opencode`, `cursor`, and `devin` are behind `--build-arg INSTALL_UNVERIFIED=true` and should be treated as best-effort until their Linux installs are verified.

## Open-model bridge for Codex

`pi`, `opencode`, and `claude` can call the open models directly through their adapters. `codex` requires a local Responses-to-Chat bridge because Codex custom providers speak the Responses API while the tested vendors expose Chat Completions.

Install the bridge once, outside the repo:

```bash
OPENBENCH_HOME=${OPENBENCH_HOME:-$HOME/.openbench}
uv venv --python 3.12 "$OPENBENCH_HOME/bridge-venv"
uv pip install --python "$OPENBENCH_HOME/bridge-venv/bin/python" 'litellm[proxy]'
```

Then start it in the foreground before Codex open-model runs:

```bash
bench/openmodel_bridge.sh
```

In another terminal, run Codex against an open model:

```bash
python3 bench/run.py \
  --harness codex \
  --task make-ci-green \
  --model deepseek-v4-flash \
  --trials 1 \
  --results-path /tmp/openbench-codex-open.jsonl
```

Security caveat: the bridge injects real provider keys upstream and, by default, binds `0.0.0.0` so Docker containers can reach it through `host.docker.internal`. Run it only on a trusted single-user machine, or set `BENCH_BRIDGE_BIND=127.0.0.1` for host-only runs.

## Reporting and transcripts

```bash
python3 bench/report.py --efficiency --results-path /tmp/openbench-pi.jsonl
```

Every real run writes a raw transcript next to the results log unless `--transcripts-dir` is overridden. Transcripts are unscrubbed and may contain paths, hostnames, emails, or echoed secrets. Before sharing any transcript:

```bash
python3 bench/scrub.py transcripts/ --check
python3 bench/scrub.py transcripts/ --out scrubbed/
python3 bench/scrub.py scrubbed/ --check
```
