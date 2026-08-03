# OpenBench — Agent Context

Read this first. It captures what this project is trying to become, so any agent
or contributor picks up the strategic context, not just the mechanics in
`README.md` / `WRITEUP.md`.

## Local execution context

Before changing benchmark execution or starting a run, read `agents.env` when
it exists. It is a gitignored, machine-local source of truth for where code is
developed and where benchmarks are executed. Never commit it or put credentials
in it.

For this installation, code changes belong in the laptop checkout. Benchmark
runs normally execute on the Mac Mini from an exact pushed commit. Do not edit
source on the Mini, do not launch from a dirty or stale checkout, and check for
active benchmark processes before starting another run.

## What OpenBench is

A benchmark framework for comparing coding-agent **harnesses** (codex, pi,
opencode, cursor, devin, claude, ...) — the CLI products that wrap a model in a
run loop, tool set, and permission policy. Tasks are self-contained
(`instruction.md` + `workspace/` + `checker.sh`); the checker is the sole judge.

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
  cloud-scale general agent evals, dataset registry, and the TB leaderboard.
  We do not compete on scale — we bridge to it (OpenBench task → Harbor task is
  a straightforward export) and keep our layer on top.
- **Prime Intellect verifiers / Environments Hub** (MIT) owns RL+eval
  environments with a package hub (versioned wheels per environment, 1k+ envs).
  Their distribution mechanics (versioned installable task packs, hub
  publishing, seeded supply via bounties) are the playbook to copy; their v1 is
  drifting toward running real CLIs, so our neutrality matters.
- **OpenBench's defensible edge:** harness-vs-harness comparison under
  realistic conditions — same-model pinning, subscription/OAuth auth handling,
  counting-proxy token metering, polarity-validated checkers
  (`validate_tasks.py`), the null negative control, and the candidate
  admission gate (`obench/candidate_gate.py`, `docs/byo-harnesses.md`). Plus an
  ultra-light stdlib-only, files-plus-shell-checker contract that non-Python
  users and private repos can adopt without learning a framework API.

## Code map (where to look)

| Area | Path |
|------|------|
| CLI entry (`obench …`) | `obench/cli.py`, `obench/__main__.py` |
| Cell runner / resume / proxy row fill | `obench/run.py` |
| Task workspace (snapshot + git archive) | `obench/workspace.py` |
| Checker polarity / validate | `obench/validate_tasks.py` |
| Task admission (structure, ownership, determinism) | `obench/admission_gate.py` |
| Candidate / BYO harness gate | `obench/candidate_gate.py`, `obench/candidates.py` |
| Report / stats / compare | `obench/report.py`, `obench/stats.py`, `obench/compare.py` |
| Publish / verify digests | `obench/publish.py` |
| Leaderboard site (harness + gateway) | `obench/site.py`, `obench/leaderboard.py`, `docs/site.md` |
| Counting proxy | `obench/proxy.py` |
| Harbor bridge | `obench/export_harbor.py`, `obench/harbor_run.py`, `obench/harbor_results.py` |
| Versioned packs (tasks + harness) | `obench/packs.py`, `docs/task-packs.md`, `docs/packs.json` |
| Stock adapters | `obench/adapters/` |
| Unit tests | `obench/tests/` |
| Tasks | `tasks/` (public), `.openbench/tasks/` (private-init), `.openbench/packs/` (installed packs) |

## Always-run CI (offline)

Match [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

```bash
pip install -e .
python3 -m unittest discover -s obench/tests -v
obench validate
```

No live harness or model-API calls; stdlib-only.

## Dangerous zones

- **`obench/run.py` `ROW_FIELDS` / append / resume** — corrupt JSONL or dropped
  fields silently skew resume and published claims; keep append fsync + fail-closed
  corrupt-line handling.
- **`exec_mode` / docker fallback** — never mix docker and local cells in one
  comparable results file (`docker_fallback` defaults off).
- **Publish digests** — `task_content_digest` must cover oracle inputs
  (`checker_data/`); verify must FAIL on missing digests.
- **`obench/report.py` aggregates** — key by `(harness, model)`, not harness alone.
- **Auth / proxy / transcripts** — auth is read-only staging; transcripts are
  LOCAL-ONLY and never published unscrubbed (`obench/scrub.py`).
- **Legacy `bench/` tree** — shims may remain; new code and docs target `obench/`.

## Roadmap (priority order)

- **P0 — Package it. [DONE Jul 2026]** `pyproject.toml`, console entry points,
  PyPI name **`obench`** (`pip install obench`, `obench run ...`). Umbrella CLI
  (`run / report / doctor / validate / gate / compare / init / publish / verify /
  pack / …`).
  CWD discovery (`tasks/`, then `.openbench/tasks/`) when run outside the repo.
- **P0 — Arbitrary task roots. [DONE Jul 2026]** `validate_tasks.py` accepts
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
- **P1 — Harbor bridge. [DONE Aug 2026]** OpenBench tasks remain the canonical
  lightweight authoring format, with deterministic conversion to Harbor 0.20
  tasks (`obench export harbor`) and Harbor-format task import
  (`obench import harbor`). The optional local Codex OAuth runner
  (`obench harbor oauth-run`) executes one exported task at a time, and
  `obench import harbor-results` fail-closed imports pinned Harbor artifacts,
  ATIF trajectories, verifier evidence, final workspaces, and agent-reported
  usage for OpenBench reporting/publication. OpenBench does not implement a
  cloud scheduler; Harbor owns sandbox execution and parallel job orchestration.
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

## Non-goals

- No cloud execution backend, no hosted leaderboard infrastructure, no RL
  training story — bridge to Harbor / verifiers instead.
- Do not abandon the stdlib-only, files-plus-shell-checker task contract; it is
  the accessibility edge.

## Working conventions for agents

- The checker is the sole judge of success; never trust harness self-reports.
- Every new task must pass `validate_tasks.py` polarity (fails untouched,
  passes with `solution/` overlaid).
- Transcripts are local-only and never published unscrubbed (`obench/scrub.py`).
- Committed datasets live under `data/`; local scratch stays in gitignored
  `results/`.
