# Provenance

- **Upstream project**: Terminal-Bench-2 (https://github.com/laude-institute/terminal-bench-2)
- **Upstream task**: `sqlite-db-truncate`
- **Upstream commit**: `2fd12b88aafdd04a52c298e3940bcb189f9766d6`
- **License**: Apache-2.0 (upstream `LICENSE`)
- **Pinned Docker image**: `alexgshaw/sqlite-db-truncate@sha256:aabac93c93bd1f310e6a6fb893911d7735026ed18491c72133c9196a09092ca4`
- **Image size (`docker inspect .Size`)**: `44172363` bytes
- **Workspace source**: exact initial `/app` contents extracted from the pinned published task image, reproducing Dockerfile COPY/RUN effects as flat files.
- **Encrypted oracle assets**: none used; no `protected.tar.gz.enc` is referenced by this task.
- **Upstream category**: `debugging`

## Conversion changes

- Copied upstream `instruction.md` verbatim.
- Placed the extracted flat-file starting state in `workspace/`.
- Copied upstream `solution/solve.sh` and materialized its deliverables in `solution/`.
- Moved upstream verifier files from `tests/` into checker-owned `checker_data/tests/`.
- Added `checker.sh`, which mounts the graded workspace at `/app`, mounts checker-owned tests read-only, emits `SCORE: <v>`, and exits 0 iff the reward is 1.

## Network-off verifier hardening

- **Derived local verifier image**: `openbench-tb2-sqlite-db-truncate:pinned`
- **Derived local image digest**: `openbench-tb2-sqlite-db-truncate@sha256:6de99b4e0c5601b952f604900e2833659dc1df03518c45a930a44d1017c7d30e`
- **Derived image size (`docker inspect .Size`)**: `71988339` bytes
- **Rebuild recipe**: `docker build --platform linux/amd64 -t openbench-tb2-sqlite-db-truncate:pinned checker_data/image`
- **Checker network policy**: `checker.sh` runs Docker with `--network none`.
- Removed runtime `apt-get`, `curl`, and uv installation from `checker_data/tests/test.sh`.
- Replaced upstream `uvx` execution with direct `pytest`; pytest 8.4.1 and pytest-json-ctrf 0.3.5 are installed in the derived image at build time.
- Exposed the build-time uv-managed Python 3.13 as `/usr/local/bin/python3` and `/usr/local/bin/python`.
