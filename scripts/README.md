# Repository utilities

Run these from the repository root after installing OpenBench (`pip install -e .`).
The supported product interface is `obench`; these scripts support development.

- `local/`: runtime builds and offline/authenticated controls. Follow each
  script's help and [campaign operations](../docs/benchmark-operations.md).
- `ci/`: credential-free hosted integration checks.
- `imports/`: task collection importers and their source registry.
- `analysis/`: analysis of saved experimental results.
- `diagnostics/`: bounded reproductions of specific execution problems.
- `check_publication_hygiene.py`: staged/tracked publication guard.

Historical one-off launchers live in `experiments/legacy/launchers/`; they are
not supported campaign entry points. Use `obench campaign` for new unattended runs.
