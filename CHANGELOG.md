# Changelog

All notable changes to OpenBench are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Local-only per-cell transcript persistence: the runner writes each cell's full
  agent transcript alongside its results row, gitignored and never published
  as-is.
- `bench/scrub.py`, a PII scrubber that replaces emails, home paths, username,
  hostnames, and key/token-shaped strings with placeholders, with a `--check`
  report mode. It never modifies originals and over-redacts on purpose.
- Import tier under `tasks-imported/`: 11 Exercism exercises converted by
  `tools/convert_exercism.py`, reusing only upstream canonical test cases (each
  records its origin and license in `provenance.json`) while instruction prose
  and reference solutions are written fresh. Kept a separate, unblended tier for
  higher contamination risk.
- Tier-aware task validation in `validate_tasks.py` (imports proved like any
  task but reported under their own tier).
- Offline CI (`.github/workflows/ci.yml`): unit tests + task-checker validation
  on every push and pull request, across Python 3.11 and 3.13, with no live
  harness or model-API calls.
- Contribution docs (`CONTRIBUTING.md`, `CONTRIBUTING-TASKS.md`) covering the
  task directory contract, the `SCORE:` partial-credit line, and the adapter
  interface.
- OSS project polish: README CI + license badges, GitHub issue templates
  (task proposal, bug report) and pull-request template, this changelog, and a
  GitHub Pages results page under `docs/`.

## [0.1.0] - 2026-07-03

First public release: a from-scratch benchmark that asks whether the coding
agent's harness matters when the model is held fixed. See
[`WRITEUP.md`](WRITEUP.md) for the full story and [`RESULTS.md`](RESULTS.md) for
the per-milestone findings.

### Added

- Benchmark harness — Python 3 standard library only, no dependencies:
  - `bench/run.py`, a resumable runner (one row per task × harness × trial) that
    copies a fresh workspace per cell and records one appended JSON line to
    `results/results.jsonl`.
  - `bench/report.py`, aggregating results into a table with Wilson 95%
    confidence intervals, mean score, wall-clock time, tokens-per-solve, and an
    `--efficiency` view.
  - `validate_tasks.py`, proving every checker fails on the untouched workspace
    and passes on the golden solution.
  - `bench/doctor.py`, a token-free preflight that checks each harness CLI,
    auth, and model-pin resolution.
  - Container-per-cell isolation (`--exec docker`) with the same adapter modules
    running unchanged, plus a local fallback.
- Five harness adapters — `codex`, `pi`, `opencode`, `cursor`, `devin` — each
  mapping the canonical `gpt-5.5-medium` to the harness's own flags, handling
  auth read-only, enforcing timeouts, and reporting tokens/turns. Plus a
  built-in `null` negative control. Contract in
  [`bench/ADAPTER_SPEC.md`](bench/ADAPTER_SPEC.md).
- Partial-credit grading via the `SCORE:` contract, so a near-miss can separate
  harnesses on harder tasks.
- Open-model support: `pi` and `opencode` wired to first-party APIs for
  `glm-5.2`, `deepseek-v4-flash`, `kimi-k2.7-code`, and free `glm-4.7-flash`.
- Benchmark tasks with validated checkers (`fix-failing-test`, `build-a-cli`,
  `make-it-run`) plus the harder partial-credit set (`make-ci-green`,
  `add-feature`, `misleading-error`) and additional originals.
- Four committed datasets and their write-ups in [`RESULTS.md`](RESULTS.md):
  M3 (`data/m3-2026-07-02/`), M3.5 (`data/m3.5-2026-07-02/`),
  M4.5 (`data/m4.5-2026-07-03/`), M4 (`data/m4-2026-07-03/`).
- Findings: on correctness, frontier harnesses on a frontier model are
  indistinguishable; they separate on efficiency (~4× wall-time, up to ~8×
  tokens per solve). Three of four open models reach frontier parity, with the
  whole 72-run open-model matrix costing about $1.02. Two summary-level
  conclusions were overturned by a per-cell look at the raw data — the
  verification discipline documented in [`WRITEUP.md`](WRITEUP.md).

[Unreleased]: https://github.com/minghinmatthewlam/openbench/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/minghinmatthewlam/openbench/releases/tag/v0.1.0
