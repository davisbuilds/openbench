# Roadmap

Selected product direction for this trunk. Future work that is not yet selected
lives in the [backlog](BACKLOG.md); upstream-bound capability follows the
[fork workflow](FORK_WORKFLOW.md).

## Product goals (the two things we are building toward)

1. **Community harness flywheel.** Make OpenBench trivially importable and
   usable so third parties can add their own harnesses or harness variations
   and evaluate them against the stock adapters. If someone builds a better
   harness or feature, they should *want* to use OpenBench to prove it and post
   the results publicly — that showing-off loop is how the framework grows.
2. **Company/private-codebase evals.** More teams evaluate agents on their own
   codebases and use cases rather than general benchmarks. OpenBench should be
   easily installable inside a private repo so companies can benchmark
   harnesses and models on *their* tasks with the same rigor (checker polarity,
   token metering, Wilson CIs) as the public tiers.

## Our niche vs. the landscape (assessed Jul 2026)

- **Harbor** (Apache-2.0, Terminal-Bench 2.0's official harness) owns
  task/trial execution, Docker isolation, retries/resume, locks, verifiers,
  ATIF artifacts, cloud-scale evals, its dataset registry, and the TB
  leaderboard. OpenBench is the suite/comparison/evidence/publication layer.
- **Prime Intellect verifiers / Environments Hub** (MIT) owns RL+eval
  environments with a package hub (versioned wheels per environment, 1k+ envs).
  Their distribution mechanics (versioned installable task packs, hub
  publishing, seeded supply via bounties) are the playbook to copy; their v1 is
  drifting toward running real CLIs, so our neutrality matters.
- **OpenBench's defensible edge:** harness-vs-harness comparison under
  realistic conditions — same-model pinning, subscription/OAuth auth handling,
  counting-proxy token metering, polarity-validated checkers
  (`obench validate`), the null negative control, and the candidate
  admission gate (`obench/candidate_gate.py`, `docs/byo-harnesses.md`). Plus an
  ultra-light stdlib-only, files-plus-shell-checker contract that non-Python
  users and private repos can adopt without learning a framework API.

## Priorities (in order)

- **P0 — Package it. [DONE Jul 2026]** `pyproject.toml`, console entry points,
  PyPI name **`obench`** (`pip install obench`, `obench run ...`). Umbrella CLI
  (`run / report / doctor / validate / gate / compare / init / publish / verify /
  pack / …`).
  Custom-project discovery (`tasks/`, then `.openbench/tasks/`) when run outside the repo.
- **P0 — Arbitrary task roots. [DONE Jul 2026]** `obench validate` accepts
  custom task directories; `--preflight-smoke` picks a smoke task from the given
  root (prefers `make-it-run` when present).
- **P0 — `obench init` for private repos. [DONE Jul 2026]** `.openbench/`
  scaffold with `openbench.toml` config defaults; git-mode workspaces
  (`workspace.toml`: repo/ref/subdir/setup, `git archive` staging, resolved
  SHA recorded as `workspace_source` provenance); `docs/private-evals.md`.
- **P1 — Show-off loop. [PARTIAL Jul 2026]** `obench publish` / `obench verify`
  ship a shareable HTML card + provenance digests (`docs/publish.md`). Still
  open: community submission path onto the public site with CI re-verifying
  digests, and seeding by porting 2–3 popular harnesses ourselves.
- **P1 — Soften allowlists. [DONE Jul 2026]** `doctor.py` discovers optional
  adapter `DOCTOR` exports (pi migrated) and accepts `--candidate` preflight;
  proxy metering for manifests is declaration-driven (`base_url_env` +
  `proxy_route`); candidate auth persist-back defaults off with
  `persist_auth = true` opt-in. Docker image's fixed CLI set remains a follow-up.
- **P1 — Harbor-first suites. [DONE Aug 2026]** `obench run [suite.toml]`
  compiles one deterministic pinned Harbor job per task set, executes all jobs,
  fail-closed imports Harbor locks/results/verifier/ATIF/workspace/usage
  evidence, and atomically seals one suite JSONL plus public and local run
  records. Exact resume is idempotent; divergent output fails.
- **P2 — Versioned packs. [DONE Jul 2026]** Task and harness packs as
  versioned, installable-by-name artifacts (`org/pack@version`) via
  `obench pack` (`init` / `install` / `list` / `verify` / `publish-index`):
  local dir, git (`git archive`), or HTTPS zip/tarball — no custom package
  server (`docs/task-packs.md`). `pack.toml` `kind = "tasks"|"harness"`;
  layout `.openbench/packs/<org>/<name>/<version>/` with `pack_source.json`
  provenance (scheme-2 task digests or per-manifest `spec_sha256`). Harness
  packs resolve as `--candidate org/name[@version][:manifest]`. Static index
  `docs/packs.json` + site Packs section; seeds under
  `data/packs/openbench-core-smoke/` and `data/packs/openbench-aider/`.
  Still open: a community hub beyond the static JSON index.
