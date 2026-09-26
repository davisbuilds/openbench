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
