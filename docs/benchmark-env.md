# OpenBench Benchmark Environment

## Overview

OpenBench runs agent-harness benchmarking cells inside disposable Docker
containers. A healthy Docker daemon with adequate CPU/memory and the correct
per-task pinned images is required for reproducible results. This document
describes the recommended environment setup, with special attention to
**Colima** (the Docker runtime used on macOS).

## Colima Orientation (for Matthew)

**Colima** is a minimal Docker Desktop alternative for macOS that runs a
Linux VM (via Lima) to host the Docker daemon. OpenBench uses Colima because
it is lightweight, scriptable, and avoids the Docker Desktop licensing
requirements.

In the incident chain that motivated this hardening, the key failure modes
were:

1. **Legacy builder no-op** — The Colima VM defaults to `legacy` builder,
   making `docker buildx` a no-op. BuildKit must be explicitly enabled.
2. **VM recreation defaults to 2 CPUs** — `colima start` without `--cpu`
   creates a 2-CPU VM, causing container timeouts under load.
3. **Image save/load corruption** — Transferring images between runtimes
   (Colima ↔ remote Docker) can produce corrupt images that pass `docker
   inspect` but fail at runtime.

### Pinned Colima Configuration

Always start Colima with the pinned resource reservation:

```bash
colima start --cpu 4 --memory 12 --disk 100
```

This matches the minimum requirements declared in
`.openbench/env-requirements.toml`:

```toml
cpus = 4
memory_gib = 12
```

### Helper Script

Use `scripts/local/colima-benchmark.sh` for a one-command setup:

```bash
bash scripts/local/colima-benchmark.sh                     # setup (idempotent)
bash scripts/local/colima-benchmark.sh --restart           # stop + start + setup
bash scripts/local/colima-benchmark.sh --status            # check VM resources
bash scripts/local/colima-benchmark.sh --profile openbench # dedicated VM (below)
bash scripts/local/colima-benchmark.sh --autostart         # + brew autostart
```

This script:
- Leaves a running VM alone when it already meets the pinned floor, and
  otherwise stops it and starts it with pinned `--cpu 4 --memory 12 --disk 100`
- With `--autostart`, enables the `brew services` colima service so the VM
  survives reboot (default profile only). It is opt-in because the VM then
  reserves its memory from every login.
- Installs the Docker Buildx plugin through Homebrew if it is missing
- Runs a smoke test (hello-world container)
- Invokes `obench doctor --docker-env` against the same VM as a final gate
- Prints the `DOCKER_CONTEXT` that runs must export to use the checked VM
  (`colima` for the default profile), since the active context may point at
  another daemon

### Shared Hosts: a Dedicated Profile

On a host whose default Colima VM serves other workloads, give benchmarks
their own VM so the pinned floor and restarts never touch those workloads:

```bash
bash scripts/local/colima-benchmark.sh --profile openbench
```

The script then starts and checks only that profile, and leaves the current
Docker context unchanged (`--activate=false`). Runs select the VM through its
Docker context, `colima-openbench`. OpenBench and Harbor both call the `docker`
CLI, so the variable reaches every container they start:

```bash
export DOCKER_CONTEXT=colima-openbench
obench doctor --docker-env
```

`obench campaign qualify` and `obench campaign launch` pass `DOCKER_CONTEXT`
from the launching shell into the campaign's tmux session. A hand-written
tmux launch script must export it itself (see
[benchmark operations](benchmark-operations.md)). The same profile can also
come from `OBENCH_COLIMA_PROFILE`. Stop the VM after a campaign to return its
memory: `colima stop --profile openbench`.

### Restart Procedure

If the benchmark environment behaves unexpectedly:

```bash
# Restart with the pinned config (add --profile NAME for a dedicated VM)
bash scripts/local/colima-benchmark.sh --restart

# Or by hand, for the default profile:
colima stop
colima status   # exits non-zero once the VM is down
colima start --cpu 4 --memory 12 --disk 100
obench doctor --docker-env
```

## The Doctor Gate

Run `obench doctor --docker-env` **before every benchmark matrix** to catch:

| Check    | What it verifies                                                      |
|----------|-----------------------------------------------------------------------|
| BUILDX   | Docker BuildKit / `buildx` plugin is installed and functional         |
| CPUS     | Docker daemon CPU count meets `env-requirements.toml` (≥ 4)           |
| MEMORY   | Docker daemon memory meets `env-requirements.toml` (≥ 12 GiB)         |
| IMAGES   | Every per-task pinned image is present **and** functionally probed    |
| AUTH     | Auth freshness per configured lane (subscription + API-key routes)    |

### Image Functional Probe

Each per-task pinned image is not only checked for local presence via
`docker image inspect`, but also **functionally probed** by running a short
exec inside it:

```bash
docker run --rm <image> python3 -c "print('ok')"
```

This catches corrupt or empty images that pass `inspect` but fail at runtime
(the image save/load corruption mode observed across runtimes).

### Configuring Resource Requirements

Edit `.openbench/env-requirements.toml` to change the floor:

```toml
cpus = 4
memory_gib = 12
```

The doctor gate reads these values and compares them against `docker info`
output. A FAIL on CPUS or MEMORY means the VM is under-provisioned — run
`colima start` with higher `--cpu` / `--memory`.

## Auth Single-Writer Rule

**Each machine does its own `pi /login` (or equivalent harness login).
Never copy `auth.json` between machines.**

Sharing auth files between machines causes OAuth token rotation conflicts
and silent credential expiration. Each benchmark host must authenticate
independently:

- **Codex**: `codex login`
- **Pi**: `pi /login`
- **Opencode**: `opencode auth login`
- **Cursor**: `cursor-agent login`
- **Devin**: `devin login`

The doctor gate's AUTH lane check verifies each credential is present and
fresh before the run starts.

## Per-Task Pinned Images

The 6 tb-mid benchmark tasks each require a pinned Docker image by digest.
These images are distributed from **GitHub Container Registry (GHCR)**:

| Task                          | GHCR Image                                                        |
|-------------------------------|-------------------------------------------------------------------|
| `adaptive-rejection-sampler`  | `ghcr.io/minghinmatthewlam/...`                                   |
| `merge-diff-arc-agi-task`     | `ghcr.io/minghinmatthewlam/...`                                   |
| `overfull-hbox`               | `ghcr.io/minghinmatthewlam/...`                                   |
| `query-optimize`              | `ghcr.io/minghinmatthewlam/...`                                   |
| `sanitize-git-repo`           | `ghcr.io/minghinmatthewlam/...`                                   |
| `winning-avg-corewars`        | `ghcr.io/minghinmatthewlam/...`                                   |

Pull by digest for reproducibility:

```bash
docker pull ghcr.io/minghinmatthewlam/<image>@sha256:<digest>
```

See `data/packs/openbench-tb-mid/images.json` for the full digest map and
`docs/ghcr-push.md` for instructions on pushing updates.

## CLI Version Pinning

Each tb-mid task image carries `/etc/openbench-cli-versions.json` with the
exact harness CLI versions baked in. These are derived from the common
harness Dockerfile ARG pins (`obench/docker/Dockerfile`):

| Harness    | Pinned Version           |
|------------|--------------------------|
| codex      | 0.144.5                  |
| pi         | 0.80.10                  |
| claude     | 2.1.214                  |
| grokbuild  | 0.2.103 (89c3d36fb6)    |
| opencode   | 1.18.3                   |
| cursor     | 2026.07.09-a3815c0      |

The version file is checked in at each task's
`image-context/etc/openbench-cli-versions.json` for provenance tracking.

## Pre-Benchmark Checklist

1. ✅ **Colima running** with pinned config (`bash scripts/local/colima-benchmark.sh`, plus `--profile NAME` and `DOCKER_CONTEXT=colima-NAME` on a shared host)
2. ✅ **`obench doctor --docker-env`** passes (BUILDX, CPUS, MEMORY, IMAGES, AUTH)
3. ✅ **Auth** configured per machine (never shared `auth.json`)
4. ✅ **Per-task images** pulled and functional (`docker run --rm <image> python3 -c "print('ok')"`)
5. ✅ **No benchmark running on the mini** (`ssh 100.66.10.106` — leave it alone)
