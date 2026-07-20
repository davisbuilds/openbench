# OpenBench — Agent Context

Read this first. It captures what this project is trying to become, so any agent
or contributor picks up the strategic context, not just the mechanics in
`README.md` / `WRITEUP.md`.

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

## Roadmap (priority order)

- **P0 — Package it.** `pyproject.toml`, real package, console entry points.
  PyPI name `openbench` is taken (Groq's eval framework); we publish as
  **`obench`** — distribution, import name, and CLI command all `obench`
  (`pip install obench`, `obench run ...`) — zero collision risk. The project
  is still called OpenBench; `obench` is just the install/command handle.
  Umbrella CLI with subcommands (`obench run / report / doctor / validate /
  gate / compare / init / publish / verify`). Git-URL installs (`pip install git+https://...`)
  work pre-PyPI. Remove repo-relative defaults (`REPO = dirname(bench)` in
  `run.py`, `report.py`, `doctor.py`, `stats.py`, ...): explicit paths/config,
  with CWD discovery (`tasks/`, then `.openbench/tasks/`) when run outside the
  repo, else a clear error.
- **P0 — Arbitrary task roots.** `validate_tasks.py` must accept custom task
  directories (today it hard-codes `tasks/` + `tasks-imported/`), and
  `--preflight-smoke` must pick a smoke task from the given root instead of
  hard-requiring the repo's `make-it-run` (`PREFLIGHT_TASK` in `run.py`).
- **P0 — `obench init` for private repos. [DONE Jul 2026]** `.openbench/`
  scaffold with `openbench.toml` config defaults; git-mode workspaces
  (`workspace.toml`: repo/ref/subdir/setup, `git archive` staging, resolved
  SHA recorded as `workspace_source` provenance); `docs/private-evals.md`.
  Packaging is also done: `pip install` (git-URL), umbrella `obench` CLI,
  arbitrary task roots for validate/preflight.
- **P1 — Show-off loop. [PARTIAL Jul 2026]** `obench publish` / `obench verify`
  ship a shareable HTML card + provenance digests (`docs/publish.md`). Still
  open: community submission path onto the public site with CI re-verifying
  digests, and seeding by porting 2–3 popular harnesses ourselves.
- **P1 — Soften allowlists. [DONE Jul 2026]** `doctor.py` discovers optional
  adapter `DOCTOR` exports (pi migrated) and accepts `--candidate` preflight;
  proxy metering for manifests is declaration-driven (`base_url_env` +
  `proxy_route`); candidate auth persist-back defaults off with
  `persist_auth = true` opt-in. Docker image's fixed CLI set remains a follow-up.
- **P1 — Harbor bridge.** One-way exporter OpenBench task → Harbor task
  (`checker.sh` + `SCORE:` → `tests/test.sh` writing `reward.txt`); lets
  companies use Harbor's cloud sandboxes while OpenBench stays the
  comparison/stats/auth layer.
- **P2 — Versioned task packs.** Task packs and harness manifests as
  versioned, installable-by-name artifacts (`org/pack@version`), following the
  verifiers hub packaging pattern.

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
