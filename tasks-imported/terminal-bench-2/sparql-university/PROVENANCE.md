# Provenance

- **Upstream project**: Terminal-Bench-2 (https://github.com/laude-institute/terminal-bench-2)
- **Upstream task**: `sparql-university`
- **Upstream commit**: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- **License**: Apache-2.0 (upstream `LICENSE`)
- **Pinned Docker image**: `alexgshaw/sparql-university@sha256:92fa4304e51167f9fb4466144eba33ef9272a7df0ac1cab9fc3986bccd1ab708`
- **Image size (`docker inspect .Size`)**: `29727705` bytes
- **Workspace source**: exact initial `/app` contents extracted from the pinned published task image, reproducing Dockerfile COPY/RUN effects as flat files.
- **Encrypted oracle assets**: none used; no `protected.tar.gz.enc` is referenced by this task.
- **Upstream category**: `data-querying`

## Conversion changes

- Copied upstream `instruction.md` verbatim.
- Placed the extracted flat-file starting state in `workspace/`.
- Copied upstream `solution/solve.sh` and materialized its deliverables in `solution/`.
- Moved upstream verifier files from `tests/` into checker-owned `checker_data/tests/`.
- Added `checker.sh`, which mounts the graded workspace at `/app`, mounts checker-owned tests read-only, emits `SCORE: <v>`, and exits 0 iff the reward is 1.

## Network-off verifier hardening

- **Derived local verifier image**: `openbench-tb2-sparql-university:pinned`
- **Derived local image digest**: `openbench-tb2-sparql-university@sha256:ee8da0571a7924ebbcc0f1e965a8d43f663643babb1bfbdc03d7877107e684ad`
- **Derived image size (`docker inspect .Size`)**: `95932892` bytes
- **Rebuild recipe**: `docker build --platform linux/amd64 -t openbench-tb2-sparql-university:pinned checker_data/image`
- **Checker network policy**: `checker.sh` runs Docker with `--network none`.
- Removed runtime `apt-get`, `curl`, and uv installation from `checker_data/tests/test.sh`.
- Replaced upstream `uvx` execution with direct `pytest`; pytest 8.4.1 and pytest-json-ctrf 0.3.5 are installed in the derived image at build time.
- Exposed the build-time uv-managed Python 3.13 as `/usr/local/bin/python3` and `/usr/local/bin/python`.
- Preinstalled upstream verifier dependency `rdflib==7.1.4` in the pytest tool environment.
- Corrected the materialized oracle query (and `solve.sh`) to apply the `> 10` enrollment threshold per department rather than incorrectly aggregating students across all departments of a professor.
