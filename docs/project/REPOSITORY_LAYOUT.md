# Repository layout

The checkout groups files by their role. Task ownership remains explicit, but
upstream history no longer dictates a separate root directory for every tier.

| Location | Owns |
| --- | --- |
| `obench/` | Installable Python code, adapters, grading implementations, and unit tests |
| `benchmarks/core/` | Portable core tasks in the legacy checker format |
| `benchmarks/imported/` | Imported task collections, grouped by source |
| `benchmarks/local/` | Fork-owned PR-derived tasks in the legacy checker format |
| `benchmarks/candidates/` | Unadmitted task candidates |
| `benchmarks/harbor/core/` | Canonical Harbor task packages |
| `benchmarks/harbor/local/` | Fork-owned Harbor repair packages |
| `scripts/` | Repository utilities: `local/`, `ci/`, `imports/`, `analysis/`, and `diagnostics/` |
| `experiments/` | Study specs, candidate configurations, repair development cases, and ablations |
| `experiments/legacy/` | Historical launchers retained as evidence, not current operating instructions |
| `docker/` | Repair runtime build inputs; package-owned legacy images remain in `obench/docker/` |
| `docs/` | Guides, reference material, project direction, reports, and the published release site |
| `data/` | Checked-in datasets, distribution packs, pricing, and historical evidence |
| `results/` | Ignored private run evidence, scratch work, and preserved worktrees/environments |
| `.openbench/` | This checkout's suite/profile configuration and ignored execution output |

## Code map

Where the main capabilities live inside `obench/` and the task trees.

| Area | Path |
|------|------|
| CLI entry (`obench …`) | `obench/cli.py`, `obench/__main__.py` |
| Canonical suite run / sealing | `obench/suite_run.py`, `obench/suite.py` |
| Legacy cell runner | `obench/run.py` |
| Task workspace (snapshot + git archive) | `obench/workspace.py` |
| Checker polarity / validate | `obench/validate_tasks.py` |
| Task admission (structure, ownership, determinism) | `obench/admission_gate.py` |
| Candidate / BYO harness gate | `obench/candidate_gate.py`, `obench/candidates.py` |
| Report / stats / compare | `obench/report.py`, `obench/stats.py`, `obench/compare.py` |
| Publish / verify digests | `obench/publish.py` |
| Leaderboard site (harness + gateway) | `obench/site.py`, `obench/leaderboard.py`, `docs/site.md` |
| Counting proxy | `obench/proxy.py` |
| Harbor jobs/results/evidence | `obench/harbor_job.py`, `obench/harbor_results.py`, `obench/harbor_run.py` |
| Versioned packs (tasks + harness) | `obench/packs.py`, `docs/task-packs.md`, `docs/packs.json` |
| Trusted repair oracles | `obench/repair_oracles/`, `obench/repair_grading.py`, `obench/repair_worker.py` |
| Stock adapters | `obench/adapters/` |
| Unit tests | `obench/tests/` |
| Tasks | `benchmarks/harbor/core/` (canonical), `benchmarks/core/` (historical compatibility), `.openbench/tasks/` (private-init) |

## Entry points and compatibility

Use `obench` and its subcommands, or `python -m obench.<module>`. The deprecated
`bench/*.py` and root `validate_tasks.py` wrappers were removed. Utility scripts
are invoked from the repository root using their path under `scripts/`.

Checkout task discovery uses `benchmarks/core`, `benchmarks/imported`, and
`benchmarks/local`. Installed/custom projects still support `tasks/`,
`tasks-local/`, `tasks-imported/`, `.openbench/tasks/`, and explicit configuration.
`obench init` retains its existing custom-project scaffold. No directory symlinks
or hidden path-rewriting layer is required.

The relocation preserves task payload bytes, reference solutions, and source
manifests. New suites use the new paths. Historical run receipts keep their
original paths and commits; use their pinned checkout for exact replay rather
than editing sealed evidence. Control-path changes require fresh runtime
qualification before the next campaign; a historical admission is not portable
by renaming its paths.

## Documentation and generated output

Start with [the documentation index](../README.md). `docs/project/` owns direction
and decisions; `docs/guides/` owns setup and authoring; `docs/reference/` owns
cross-cutting contracts; `docs/reports/` holds long-form historical analysis.
The published site remains rooted at `docs/`, with existing release URLs and
sealed artifacts intact. Generated site output is not a second source tree.

Keep root files to the README, agent/contribution/security guidance, licensing,
and tool configuration. Put new experimental scripts with the maintained tools
under `scripts/`; put their inputs and study-specific notes under `experiments/`.
